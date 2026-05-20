import os
import json
import logging
import asyncio
import uuid
import threading
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Blueprint, request, jsonify, render_template, session
from .blockchain import learn_blockchain_service
# Contract integration removed - using direct private key disbursement only
from supabase_client import get_supabase_client
from config import (
    GOODDOLLAR_CONTRACT_ADDRESS as _GD_CONTRACT_ADDRESS,
    LEARN_EARN_CONTRACT_ADDRESS as _CONFIG_LEARN_EARN_ADDRESS,
    ACHIEVEMENT_NFT_CONTRACT_ADDRESS as _CONFIG_NFT_ADDRESS,
)
import random
import time
from typing import Dict, Any
from decimal import Decimal

logger = logging.getLogger(__name__)

# Create Learn & Earn Blueprint
learn_earn_bp = Blueprint('learn_earn', __name__, url_prefix='/learn-earn')

_nft_purchase_memory_jobs = {}
_nft_purchase_memory_lock = threading.Lock()
_gd_decimals_cache: int | None = None

# ── In-memory caches ──────────────────────────────────────────────────────────
_QUIZ_QUESTIONS_TTL = 300   # 5 minutes  – questions rarely change
_NFT_BALANCE_TTL    = 30    # 30 seconds – blockchain call, per user
_MARKETPLACE_TTL    = 30    # 30 seconds – changes on list/delist/buy
_MY_NFTS_TTL        = 30    # 30 seconds – per user
_QUIZ_HISTORY_TTL   = 60    # 60 seconds – per user, changes after quiz submit
_CARD_SALES_TTL     = 60    # 60 seconds – per user, changes after card sale

_quiz_questions_cache: dict = {"data": None, "expires": 0}   # shared (no user key)
_nft_balance_cache:    dict = {}   # {wallet: (data, expires)}
_marketplace_cache:    dict = {"data": None, "expires": 0}
_my_nfts_cache:        dict = {}   # {wallet: (data, expires)}
_quiz_history_cache:   dict = {}   # {wallet: (data, expires)}
_card_sales_cache:     dict = {}   # {wallet: (data, expires)}
# ─────────────────────────────────────────────────────────────────────────────

COLLABORATION_MIN_GD = int(os.getenv('COLLABORATION_MIN_GD', '100000'))


STREAMING_DURATION_SECONDS = int(os.getenv('LEARN_EARN_STREAM_DURATION_SECONDS', '86400'))
STREAMING_PAYOUT_MODE = (os.getenv('LEARN_EARN_PAYOUT_MODE', 'instant') or 'instant').strip().lower()
STREAMING_MODE_ALIASES = {'stream_1day', 'stream', 'streaming', 'stream_payout'}


def _is_streaming_mode(mode: str) -> bool:
    return (mode or '').strip().lower() in STREAMING_MODE_ALIASES


def _is_streaming_runtime_ready() -> tuple[bool, str | None]:
    """Return (ready, reason) for the Superfluid stream runtime.

    The streaming code path needs three things to actually settle on-chain:
    - Superfluid host + CFA addresses (or createFlow has nowhere to go),
    - a stream token address (SuperToken-compatible),
    - a configured sender wallet on the blockchain service.

    If any are missing we report not-ready so callers can fall back to the
    instant reward instead of queuing rows that will never be settled.
    """
    host = os.getenv('SUPERFLUID_HOST_ADDRESS')
    cfa = os.getenv('SUPERFLUID_CFA_V1_ADDRESS')
    token = (
        os.getenv('LEARN_EARN_STREAM_TOKEN_ADDRESS')
        or os.getenv('GOODDOLLAR_SUPERTOKEN_ADDRESS')
    )
    if not host:
        return False, 'SUPERFLUID_HOST_ADDRESS missing'
    if not cfa:
        return False, 'SUPERFLUID_CFA_V1_ADDRESS missing'
    if not token:
        return False, 'LEARN_EARN_STREAM_TOKEN_ADDRESS / GOODDOLLAR_SUPERTOKEN_ADDRESS missing'
    sender = getattr(getattr(learn_blockchain_service, 'owner_account', None), 'address', None)
    if not sender or not getattr(learn_blockchain_service, '_wallet_key', None):
        return False, 'learn_blockchain_service wallet not configured'
    return True, None


class LearnEarnStreamingService:
    """Manage Learn & Earn streaming rows and lifecycle orchestration data in Supabase."""

    def __init__(self):
        self.stream_token_address = (
            os.getenv('LEARN_EARN_STREAM_TOKEN_ADDRESS')
            or os.getenv('GOODDOLLAR_SUPERTOKEN_ADDRESS')
            or _get_gooddollar_contract_address()
        )

    @staticmethod
    def is_runtime_ready() -> tuple[bool, str | None]:
        return _is_streaming_runtime_ready()

    @staticmethod
    def compute_flow_rate_wei(amount_gd: float, duration_seconds: int) -> int:
        d_amount = Decimal(str(amount_gd))
        wei_amount = int(d_amount * Decimal('1000000000000000000'))
        if duration_seconds <= 0:
            raise ValueError('duration_seconds must be positive')
        return max(1, wei_amount // duration_seconds)

    def create_stream_job(self, user_wallet: str, reward_amount: float, reward_id: str | None = None) -> dict:
        supabase = get_supabase_client()
        if not supabase:
            return {'success': False, 'error': 'Database not available'}

        now = datetime.now(timezone.utc)
        end_at = now + timedelta(seconds=STREAMING_DURATION_SECONDS)
        flow_rate_wei = self.compute_flow_rate_wei(reward_amount, STREAMING_DURATION_SECONDS)
        sender_wallet = (getattr(learn_blockchain_service.owner_account, 'address', None) or '').lower()
        idempotency_key = f"{user_wallet.lower()}:{reward_id or uuid.uuid4().hex}:{int(now.timestamp())}"

        payload = {
            'reward_id': reward_id,
            'user_wallet': user_wallet.lower(),
            'amount_gd': reward_amount,
            'duration_seconds': STREAMING_DURATION_SECONDS,
            'flow_rate_wei': str(flow_rate_wei),
            'stream_token_address': self.stream_token_address.lower(),
            'sender_wallet': sender_wallet,
            'start_at': now.isoformat(),
            'end_at': end_at.isoformat(),
            'status': 'pending_start',
            'idempotency_key': idempotency_key,
        }

        try:
            result = supabase.table('learn_earn_streams').insert(payload).execute()
            row = (result.data or [{}])[0]
            return {'success': True, 'stream': row, 'flow_rate_wei': flow_rate_wei}
        except Exception as e:
            logger.error(f"❌ create_stream_job error: {e}")
            return {'success': False, 'error': str(e)}

    def mark_stream_active(self, stream_id: str, tx_hash: str) -> None:
        supabase = get_supabase_client()
        if not supabase:
            return
        supabase.table('learn_earn_streams').update({
            'status': 'active',
            'create_tx_hash': tx_hash,
            'last_error': None
        }).eq('id', stream_id).execute()

    def mark_stream_failed(self, stream_id: str, stage: str, error: str) -> None:
        supabase = get_supabase_client()
        if not supabase:
            return
        status = 'start_failed' if stage == 'start' else 'stop_failed'
        supabase.table('learn_earn_streams').update({
            'status': status,
            'last_error': error,
            'retry_count': 1
        }).eq('id', stream_id).execute()

    def _claim_row(self, supabase, row: dict, expected_status: str) -> bool:
        """Atomically claim a stream row by bumping retry_count via OCC.

        Returns True if this worker won the claim. Multiple gunicorn workers
        may race on the same row; only one will succeed because the WHERE
        clause matches the observed retry_count value.
        """
        row_id = row.get('id')
        if not row_id:
            return False
        observed_retry = int(row.get('retry_count') or 0)
        try:
            resp = supabase.table('learn_earn_streams').update({
                'retry_count': observed_retry + 1,
                'last_error': f"inflight:{expected_status}:{datetime.now(timezone.utc).isoformat()}",
            }).eq('id', row_id).eq('status', expected_status).eq('retry_count', observed_retry).execute()
            return bool(resp.data)
        except Exception as e:
            logger.warning(f"⚠️ stream claim failed for {row_id}: {e}")
            return False

    def _start_one(self, supabase, row: dict) -> bool:
        """Submit createFlow for one claimed pending_start row. Returns success."""
        row_id = row['id']
        try:
            flow_rate_wei = int(row.get('flow_rate_wei') or 0)
        except (TypeError, ValueError):
            flow_rate_wei = 0
        if flow_rate_wei <= 0:
            supabase.table('learn_earn_streams').update({
                'status': 'start_failed',
                'last_error': 'invalid flow_rate_wei',
            }).eq('id', row_id).execute()
            return False
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                learn_blockchain_service.start_reward_stream(row['user_wallet'], flow_rate_wei)
            )
        except Exception as e:
            result = {'success': False, 'error': str(e)}
        finally:
            loop.close()

        if result.get('success'):
            supabase.table('learn_earn_streams').update({
                'status': 'active',
                'create_tx_hash': result.get('tx_hash'),
                'last_error': None,
            }).eq('id', row_id).execute()
            return True
        supabase.table('learn_earn_streams').update({
            'status': 'start_failed',
            'last_error': (result.get('error') or 'start failed')[:500],
        }).eq('id', row_id).execute()
        return False

    def _stop_one(self, supabase, row: dict) -> bool:
        """Submit deleteFlow for one claimed due row. Returns success."""
        row_id = row['id']
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                learn_blockchain_service.stop_reward_stream(row['user_wallet'])
            )
        except Exception as e:
            result = {'success': False, 'error': str(e)}
        finally:
            loop.close()

        if result.get('success'):
            supabase.table('learn_earn_streams').update({
                'status': 'stopped',
                'stop_tx_hash': result.get('tx_hash'),
                'last_error': None,
            }).eq('id', row_id).execute()
            return True
        supabase.table('learn_earn_streams').update({
            'status': 'stop_failed',
            'last_error': (result.get('error') or 'stop failed')[:500],
        }).eq('id', row_id).execute()
        return False

    def process_streams_once(self, start_limit: int = 50, stop_limit: int = 100) -> dict:
        """Single processing cycle: start queued streams + stop due streams.

        Safe to call from both the manual /process-streams endpoint and the
        in-process scheduler. Each row is claimed atomically via OCC so two
        workers racing on the same row will not double-submit on-chain.
        """
        supabase = get_supabase_client()
        if not supabase:
            return {'success': False, 'error': 'Database not available',
                    'started': 0, 'stopped': 0, 'failed': 0, 'checked': 0}

        ready, reason = self.is_runtime_ready()
        if not ready:
            return {'success': False, 'error': f'Stream runtime not ready: {reason}',
                    'started': 0, 'stopped': 0, 'failed': 0, 'checked': 0}

        started = stopped = failed = checked = 0

        try:
            pending = supabase.table('learn_earn_streams').select('*') \
                .eq('status', 'pending_start') \
                .order('created_at', desc=False) \
                .limit(start_limit).execute().data or []
        except Exception as e:
            logger.warning(f"⚠️ fetch pending_start failed: {e}")
            pending = []

        for row in pending:
            checked += 1
            if not self._claim_row(supabase, row, 'pending_start'):
                continue
            if self._start_one(supabase, row):
                started += 1
            else:
                failed += 1

        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            due = supabase.table('learn_earn_streams').select('*') \
                .in_('status', ['active', 'pending_stop', 'stop_failed']) \
                .lte('end_at', now_iso) \
                .order('end_at', desc=False) \
                .limit(stop_limit).execute().data or []
        except Exception as e:
            logger.warning(f"⚠️ fetch due stops failed: {e}")
            due = []

        for row in due:
            checked += 1
            if not self._claim_row(supabase, row, row.get('status') or 'active'):
                continue
            if self._stop_one(supabase, row):
                stopped += 1
            else:
                failed += 1

        return {'success': True, 'started': started, 'stopped': stopped,
                'failed': failed, 'checked': checked}


def _get_gooddollar_contract_address() -> str:
    """Resolve GoodDollar token contract with backwards-compatible env keys."""
    return (
        os.getenv('GOODDOLLAR_CONTRACT_ADDRESS')
        or os.getenv('GOODDOLLAR_CONTRACT')
        or _GD_CONTRACT_ADDRESS
        or '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A'
    )


def _get_gooddollar_decimals(w3, token_address: str) -> int:
    """Read token decimals with a safe fallback to 18."""
    global _gd_decimals_cache
    if _gd_decimals_cache is not None:
        return _gd_decimals_cache

    try:
        token_contract = w3.eth.contract(
            address=w3.to_checksum_address(token_address),
            abi=[
                {
                    "constant": True,
                    "inputs": [],
                    "name": "decimals",
                    "outputs": [{"name": "", "type": "uint8"}],
                    "type": "function",
                }
            ],
        )
        token_decimals = int(token_contract.functions.decimals().call())
        if token_decimals < 0 or token_decimals > 36:
            raise ValueError(f"Unexpected token decimals: {token_decimals}")
        _gd_decimals_cache = token_decimals
    except Exception as decimals_err:
        logger.warning(f"⚠️ Falling back to 18 decimals for G$ amount parsing: {decimals_err}")
        _gd_decimals_cache = 18

    return _gd_decimals_cache


def _wallet_from_session_or_request(data: dict) -> str:
    wallet = (session.get('wallet') or '').strip()
    if wallet:
        return wallet
    return (data.get('wallet_address') or '').strip()


def _normalize_wallet(wallet: str) -> str:
    return (wallet or '').strip().lower()


def _is_missing_nft_purchase_jobs_error(error) -> bool:
    text = str(error)
    if 'nft_purchase_jobs' not in text:
        return False
    missing_indicators = (
        'PGRST205',
        'Could not find the table',
        'relation "nft_purchase_jobs" does not exist',
        'undefined table',
        'does not exist',
        '42P01',
    )
    return any(ind in text for ind in missing_indicators)


def _set_memory_nft_job(job_id, buyer_wallet=None, status=None, result=None, error=None):
    with _nft_purchase_memory_lock:
        job = _nft_purchase_memory_jobs.get(job_id, {})
        if buyer_wallet is not None:
            job['buyer_wallet'] = buyer_wallet
        if status is not None:
            job['status'] = status
        if result is not None:
            job['result'] = result
        if error is not None:
            job['error_message'] = error
        job['updated_at'] = datetime.now(timezone.utc).isoformat()
        _nft_purchase_memory_jobs[job_id] = job


def _get_memory_nft_job(job_id, buyer_wallet):
    with _nft_purchase_memory_lock:
        job = _nft_purchase_memory_jobs.get(job_id)
        if not job or not _wallets_match(job.get('buyer_wallet'), buyer_wallet):
            return None
        return dict(job)



streaming_service = LearnEarnStreamingService()

class LearnEarnQuizManager:
    def __init__(self):
        self.questions_per_quiz = 10
        self.time_per_question = 20
        self.max_reward_per_quiz = 2000
        self.max_retries = 3
        self.cooldown_hours = 120

        self.load_quiz_settings()

    @property
    def reward_per_correct(self):
        return self.max_reward_per_quiz / self.questions_per_quiz

    def load_quiz_settings(self):
        try:
            supabase = get_supabase_client()
            if not supabase:
                logger.warning("⚠️ Supabase not available - using default quiz settings")
                return

            result = supabase.table('quiz_settings').select('*').limit(1).execute()

            if result.data and len(result.data) > 0:
                settings = result.data[0]
                self.questions_per_quiz = settings.get('questions_per_quiz', 10)
                self.time_per_question = settings.get('time_per_question', 20)
                self.max_reward_per_quiz = settings.get('max_reward_per_quiz', 2000)
                logger.info(f"✅ Loaded quiz settings: {self.questions_per_quiz} questions, {self.time_per_question}s per question, {self.max_reward_per_quiz} G$ max reward")
            else:
                # Create default settings if none exist
                default_settings = {
                    'questions_per_quiz': 10,
                    'time_per_question': 20,
                    'max_reward_per_quiz': 2000
                }
                supabase.table('quiz_settings').insert(default_settings).execute()
                logger.info("✅ Created default quiz settings in database")
        except Exception as e:
            logger.error(f"❌ Error loading quiz settings: {e}")

    def get_quiz_settings(self):
        return {
            'questions_per_quiz': self.questions_per_quiz,
            'time_per_question': self.time_per_question,
            'max_reward_per_quiz': self.max_reward_per_quiz,
            'reward_per_correct': self.reward_per_correct
        }

    def update_quiz_settings(self, questions_per_quiz=None, time_per_question=None, max_reward_per_quiz=None):
        try:
            supabase = get_supabase_client()
            if not supabase:
                return {'success': False, 'error': 'Database not available'}

            update_data = {}
            if questions_per_quiz is not None:
                update_data['questions_per_quiz'] = questions_per_quiz
                self.questions_per_quiz = questions_per_quiz
            if time_per_question is not None:
                update_data['time_per_question'] = time_per_question
                self.time_per_question = time_per_question
            if max_reward_per_quiz is not None:
                update_data['max_reward_per_quiz'] = max_reward_per_quiz
                self.max_reward_per_quiz = max_reward_per_quiz

            if not update_data:
                return {'success': False, 'error': 'No settings to update'}

            # Update or insert settings
            result = supabase.table('quiz_settings').select('*').limit(1).execute()

            if result.data and len(result.data) > 0:
                # Update existing settings
                supabase.table('quiz_settings').update(update_data).eq('id', result.data[0]['id']).execute()
            else:
                # Insert new settings
                supabase.table('quiz_settings').insert(update_data).execute()

            logger.info(f"✅ Updated quiz settings: {update_data}")
            return {'success': True, 'settings': self.get_quiz_settings()}
        except Exception as e:
            logger.error(f"❌ Error updating quiz settings: {e}")
            return {'success': False, 'error': str(e)}

    async def initialize_sample_questions(self):
        try:
            supabase = get_supabase_client()

            if supabase is None:
                logger.warning("⚠️ Supabase not configured - skipping question initialization")
                return

            # Check if questions exist
            existing_result = supabase.table('quiz_questions').select('*').limit(1).execute()

            if len(existing_result.data) > 0:
                logger.info("📚 Learn questions already exist in Supabase")
                return

            # Create sample questions (removed category and difficulty columns)
            sample_questions = [
                {
                    'question_id': 'Q001',
                    'question': 'What is GoodDollar (G$)?',
                    'answer_a': 'A cryptocurrency for universal basic income',
                    'answer_b': 'A regular bank currency',
                    'answer_c': 'A credit card company',
                    'answer_d': 'A shopping website',
                    'correct': 'A',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q002',
                    'question': 'How often can you claim UBI with GoodDollar?',
                    'answer_a': 'Once per month',
                    'answer_b': 'Daily',
                    'answer_c': 'Once per year',
                    'answer_d': 'Only once',
                    'correct': 'B',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q003',
                    'question': 'What blockchain network does GoodDollar use?',
                    'answer_a': 'Bitcoin',
                    'answer_b': 'Ethereum',
                    'answer_c': 'Celo',
                    'answer_d': 'Binance Smart Chain',
                    'correct': 'C',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q004',
                    'question': 'What is the main goal of GoodDollar?',
                    'answer_a': 'Make money for investors',
                    'answer_b': 'Provide universal basic income',
                    'answer_c': 'Replace all banks',
                    'answer_d': 'Create a gaming platform',
                    'correct': 'B',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q005',
                    'question': 'Where can you claim your GoodDollar UBI?',
                    'answer_a': 'goodmarket.com',
                    'answer_b': 'goodwallet.xyz',
                    'answer_c': 'facebook.com',
                    'answer_d': 'google.com',
                    'correct': 'B',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q006',
                    'question': 'What happens if you don\'t claim UBI for 7+ days?',
                    'answer_a': 'Nothing changes',
                    'answer_b': 'You lose access to Learn & Earn rewards',
                    'answer_c': 'Your wallet gets deleted',
                    'answer_d': 'You get bonus rewards',
                    'correct': 'B',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q007',
                    'question': 'How many G$ do you earn per correct answer in Learn & Earn?',
                    'answer_a': '100 G$',
                    'answer_b': '200 G$',
                    'answer_c': '300 G$',
                    'answer_d': '500 G$',
                    'correct': 'B',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q008',
                    'question': 'What is the Celo network chain ID?',
                    'answer_a': '1',
                    'answer_b': '56',
                    'answer_c': '42220',
                    'answer_d': '137',
                    'correct': 'C',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q009',
                    'question': 'How many questions are in each Learn & Earn quiz?',
                    'answer_a': '5 questions',
                    'answer_b': '10 questions',
                    'answer_c': '15 questions',
                    'answer_d': '20 questions',
                    'correct': 'B',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q010',
                    'question': 'How long do you have to answer each question?',
                    'answer_a': '10 seconds',
                    'answer_b': '20 seconds',
                    'answer_c': '30 seconds',
                    'answer_d': '1 minute',
                    'correct': 'B',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q011',
                    'question': 'What is financial inclusion?',
                    'answer_a': 'Only rich people can use money',
                    'answer_b': 'Everyone has access to financial services',
                    'answer_c': 'Banks control all money',
                    'answer_d': 'Cryptocurrency is illegal',
                    'correct': 'B',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                },
                {
                    'question_id': 'Q012',
                    'question': 'What makes GoodDollar different from Bitcoin?',
                    'answer_a': 'GoodDollar is for universal basic income',
                    'answer_b': 'Bitcoin is faster',
                    'answer_c': 'GoodDollar uses more energy',
                    'answer_d': 'Bitcoin is free',
                    'correct': 'A',
                    'created_at': datetime.utcnow().isoformat() + 'Z'  # Use UTC with Z suffix
                }
            ]

            # Save questions to Supabase one by one to handle any individual failures
            for question in sample_questions:
                try:
                    result = supabase.table('quiz_questions').insert(question).execute()
                    logger.info(f"✅ Added question {question['question_id']}")
                except Exception as e:
                    logger.error(f"❌ Failed to add question {question['question_id']}: {e}")

            logger.info(f"✅ Initialized {len(sample_questions)} sample questions in Supabase")

        except Exception as e:
            logger.error(f"❌ Error initializing sample questions: {e}")

    async def get_random_questions(self, count=10):
        try:
            supabase = get_supabase_client()

            if supabase is None:
                logger.warning("⚠️ Supabase not configured - returning empty questions")
                return []

            # ── Cache check: return cached full list if still fresh ────────────
            now = time.time()
            if _quiz_questions_cache["data"] is not None and now < _quiz_questions_cache["expires"]:
                all_questions = _quiz_questions_cache["data"]
                logger.info(f"📦 Using cached quiz questions ({len(all_questions)} total)")
            else:
                # Get all questions from DB and cache them
                result = supabase.table('quiz_questions').select('*').execute()
                all_questions = result.data
                _quiz_questions_cache["data"] = all_questions
                _quiz_questions_cache["expires"] = now + _QUIZ_QUESTIONS_TTL
                logger.info(f"🔄 Fetched {len(all_questions)} quiz questions from DB (cached for {_QUIZ_QUESTIONS_TTL}s)")

            if len(all_questions) < count:
                logger.warning(f"⚠️ Not enough questions in database: {len(all_questions)} < {count}")
                # Initialize questions if none exist
                if len(all_questions) == 0:
                    await self.initialize_sample_questions()
                    # Retry getting questions
                    result = supabase.table('quiz_questions').select('*').execute()
                    all_questions = result.data
                    # Update cache with freshly initialized questions
                    _quiz_questions_cache["data"] = all_questions
                    _quiz_questions_cache["expires"] = time.time() + _QUIZ_QUESTIONS_TTL
                selected_questions = all_questions
            else:
                # Randomly select questions
                selected_questions = random.sample(all_questions, count)

            # Format questions for quiz
            quiz_questions = []
            for i, question in enumerate(selected_questions):
                quiz_questions.append({
                    'question_number': i + 1,
                    'question_id': question['question_id'],
                    'question': question['question'],
                    'options': [question['answer_a'], question['answer_b'], question['answer_c'], question['answer_d']],
                    'correct_answer': ord(question['correct']) - ord('A'),  # Convert A,B,C,D to 0,1,2,3
                    'category': 'general',  # Default category since column doesn't exist
                    'difficulty': 'medium'  # Default difficulty since column doesn't exist
                })

            logger.info(f"📚 Selected {len(quiz_questions)} questions for quiz")
            return quiz_questions

        except Exception as e:
            logger.error(f"❌ Error getting random questions: {e}")
            return []

    def mask_wallet_address(self, wallet_address: str) -> str:
        if not wallet_address.startswith("0x") or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def get_next_quiz_time(self, wallet_address: str) -> Dict[str, Any]:
        """Get the timestamp of the last quiz attempt for a user"""
        try:
            supabase = get_supabase_client()
            masked_address = self.mask_wallet_address(wallet_address)

            # Fetch the most recent quiz attempt for the user
            result = supabase.table('learnearn_log')\
                .select('timestamp')\
                .eq('wallet_address', masked_address)\
                .order('timestamp', desc=True)\
                .limit(1)\
                .execute()

            if result.data:
                last_attempt_str = result.data[0]['timestamp']

                # Handle timezone-aware datetime from Supabase - ensure UTC parsing
                try:
                    # Parse as UTC datetime consistently
                    if last_attempt_str.endswith('Z'):
                        # Already UTC with Z suffix - parse directly
                        last_attempt_time = datetime.fromisoformat(last_attempt_str.replace('Z', '+00:00')).replace(tzinfo=None)
                    elif '+' in last_attempt_str or '-' in last_attempt_str[-6:]:
                        # Has timezone offset - convert to UTC
                        dt_with_tz = datetime.fromisoformat(last_attempt_str)
                        last_attempt_time = dt_with_tz.utctimetuple()
                        last_attempt_time = datetime(*last_attempt_time[:6])  # Convert to naive UTC
                    else:
                        # Assume naive UTC datetime from Supabase
                        last_attempt_time = datetime.fromisoformat(last_attempt_str)
                except Exception as parse_error:
                    logger.error(f"❌ Error parsing UTC timestamp in next quiz time check: {parse_error}")
                    logger.error(f"Original timestamp: {last_attempt_str}")
                    # If parsing fails, assume user can take quiz
                    return {
                        'next_quiz_time': None,
                        'can_take_now': True
                    }

                # Use the configured cooldown hours (120 hours = 5 days)
                next_quiz_time = last_attempt_time + timedelta(hours=self.cooldown_hours)
                current_utc_time = datetime.utcnow() # Use UTC time
                can_take_now = current_utc_time >= next_quiz_time

                logger.info(f"🕐 Next quiz time check for {masked_address} (UTC):")
                logger.info(f"📅 Last attempt: {last_attempt_time}")
                logger.info(f"📅 Next quiz time: {next_quiz_time}")
                logger.info(f"📅 Current time: {current_utc_time}")
                logger.info(f"⏰ Cooldown: {self.cooldown_hours} hours")
                logger.info(f"✅ Can take now: {can_take_now}")

                return {
                    'next_quiz_time': next_quiz_time.isoformat(),
                    'can_take_now': can_take_now
                }
            else:
                # No previous attempts, user can take quiz immediately
                return {
                    'next_quiz_time': None,
                    'can_take_now': True
                }
        except Exception as e:
            logger.error(f"❌ Error getting next quiz time for {wallet_address}: {e}")
            # Assume user can take quiz if error occurs during retrieval
            return {
                'next_quiz_time': None,
                'can_take_now': True
            }

    async def save_quiz_attempt(self, user_wallet, questions, user_answers, total_reward, ubi_verification, retry_count=0):
        """Save quiz attempt to Supabase with retry logic - ONLY when reward is successfully sent"""
        try:
            supabase = get_supabase_client()
            quiz_id = f"QUIZ_{user_wallet.lower()}_{datetime.utcnow().isoformat()}"

            # Calculate results
            correct_answers = 0
            answer_details = []

            for i, question in enumerate(questions):
                user_answer = user_answers[i]
                is_correct = user_answer == question['correct_answer']
                if is_correct:
                    correct_answers += 1

                answer_details.append({
                    'question_number': i + 1,
                    'question_id': question['question_id'],
                    'question': question['question'],
                    'user_answer': user_answer,
                    'correct_answer': question['correct_answer'],
                    'is_correct': is_correct,
                    'category': question.get('category', 'general')
                })

            # Create quiz log entry with timezone-naive timestamp
            current_time = datetime.utcnow()
            quiz_log = {
                'quiz_id': quiz_id,
                'wallet_address': self.mask_wallet_address(user_wallet),
                'timestamp': current_time.isoformat() + 'Z',  # Use UTC with Z suffix
                'score': correct_answers,
                'total_questions': len(questions),
                'amount_g$': total_reward,
                'status': True,
                'answers': json.dumps(answer_details),  # Store as JSON string
                'ubi_verification': json.dumps(ubi_verification),
                'blocked': ubi_verification.get('blocked', False)
            }

            # Save to Supabase
            result = supabase.table('learnearn_log').insert(quiz_log).execute()

            logger.info(f"✅ Quiz attempt saved: {quiz_id} - Score: {correct_answers}/{len(questions)} - Timestamp: {current_time.isoformat()}")
            return quiz_log

        except Exception as e:
            logger.error(f"❌ Error saving quiz attempt (attempt {retry_count + 1}): {e}")

            # Retry logic
            if retry_count < self.max_retries:
                logger.info(f"🔄 Retrying quiz save... ({retry_count + 1}/{self.max_retries})")
                await asyncio.sleep(2 ** retry_count)
                return await self.save_quiz_attempt(user_wallet, questions, user_answers, total_reward, ubi_verification, retry_count + 1)
            else:
                logger.error(f"❌ Quiz save failed after {retry_count + 1} attempts")
                return None

    def check_user_eligibility(self, wallet_address: str) -> bool:
        """Check user eligibility based on last quiz attempt timestamp.
           Returns True if eligible, False otherwise."""
        try:
            supabase = get_supabase_client()
            masked_address = self.mask_wallet_address(wallet_address)

            # Fetch the most recent quiz attempt for the user
            result = supabase.table('learnearn_log')\
                .select('timestamp')\
                .eq('wallet_address', masked_address)\
                .order('timestamp', desc=True)\
                .limit(1)\
                .execute()

            if result.data:
                last_attempt_str = result.data[0]['timestamp']

                # Handle timezone-aware datetime from Supabase - ensure UTC parsing
                try:
                    # Parse as UTC datetime consistently
                    if last_attempt_str.endswith('Z'):
                        # Already UTC with Z suffix - parse directly
                        last_attempt_time = datetime.fromisoformat(last_attempt_str.replace('Z', '+00:00')).replace(tzinfo=None)
                    elif '+' in last_attempt_str or '-' in last_attempt_str[-6:]:
                        # Has timezone offset - convert to UTC
                        dt_with_tz = datetime.fromisoformat(last_attempt_str)
                        last_attempt_time = dt_with_tz.utctimetuple()
                        last_attempt_time = datetime(*last_attempt_time[:6])  # Convert to naive UTC
                    else:
                        # Assume naive UTC datetime from Supabase
                        last_attempt_time = datetime.fromisoformat(last_attempt_str)
                except Exception as parse_error:
                    logger.error(f"❌ Error parsing UTC timestamp in eligibility check: {parse_error}")
                    logger.error(f"Original timestamp: {last_attempt_str}")
                    # If parsing fails, assume user can take quiz
                    return True

                # Calculate using UTC time consistently
                next_quiz_time = last_attempt_time + timedelta(hours=self.cooldown_hours)
                current_utc_time = datetime.utcnow()
                can_take_now = current_utc_time >= next_quiz_time

                if can_take_now:
                    logger.info(f"✅ User {masked_address} is eligible for quiz (UTC check).")
                else:
                    logger.warning(f"⚠️ User {masked_address} is not eligible for quiz. UTC cooldown active until {next_quiz_time}")

                logger.info(f"🕐 UTC Eligibility Check - Last: {last_attempt_time}, Next: {next_quiz_time}, Current: {current_utc_time}")

                return can_take_now
            else:
                # No previous attempts, user can take quiz immediately
                logger.info(f"✅ User {masked_address} is eligible for quiz (first time).")
                return True

        except Exception as e:
            logger.error(f"❌ Failed to check eligibility for {wallet_address}: {e}")
            # Default to eligible if there's an error during checking
            return True

    async def check_quiz_eligibility(self, wallet_address: str) -> Dict[str, Any]:
        """Check if user is eligible for Learn & Earn quiz (24-hour cooldown only)"""
        try:
            # Check maintenance mode first
            try:
                from maintenance_service import maintenance_service
                maintenance_status = maintenance_service.get_maintenance_status('learn_earn')

                if maintenance_status.get('is_maintenance'):
                    logger.info(f"⚠️ Learn & Earn is under maintenance")
                    return {
                        'eligible': False,
                        'blocked': True,
                        'reason': 'maintenance_mode',
                        'message': maintenance_status.get('message', 'Learn & Earn is currently under maintenance. Please try again later.'),
                        'can_take_now': False,
                        'feature_available': False,
                        'maintenance': True
                    }
            except Exception as maint_error:
                logger.warning(f"⚠️ Maintenance check failed: {maint_error}")
                # Continue with eligibility check even if maintenance check fails

            # Check using sync method like hour_bonus
            try:
                eligible = self.check_user_eligibility(wallet_address)
            except Exception as elig_error:
                logger.error(f"❌ Eligibility check error: {elig_error}")
                # Default to eligible if check fails
                eligible = True

            # Get next quiz time for consistent data
            try:
                next_quiz_info = await self.get_next_quiz_time(wallet_address)
                can_take_now = next_quiz_info.get('can_take_now', eligible)
            except Exception as time_error:
                logger.error(f"❌ Next quiz time check error: {time_error}")
                # Default to using eligibility result
                next_quiz_info = {'can_take_now': eligible, 'next_quiz_time': None}
                can_take_now = eligible

            if not eligible or not can_take_now:
                return {
                    'eligible': False,
                    'blocked': True,
                    'reason': '5-day cooldown active',
                    'message': 'You have already completed a quiz in the last 5 days. Please wait before taking another quiz.',
                    'next_quiz_time': next_quiz_info.get('next_quiz_time'),
                    'can_take_now': False,
                    'cooldown_hours': 120,
                    'feature_available': True  # Feature is available, just on cooldown
                }

            # User is eligible and can take quiz now
            logger.info(f"✅ User {wallet_address} is eligible for Learn & Earn quiz")
            return {
                'eligible': True,
                'blocked': False,
                'reason': 'No recent quiz completion found',
                'message': 'You can take the quiz and earn instant G$ rewards!',
                'max_reward': 0,
                'can_take_now': True,
                'cooldown_hours': 120,
                'feature_available': True
            }

        except Exception as e:
            logger.error(f"❌ Error checking Learn & Earn quiz eligibility: {e}")
            import traceback
            logger.error(f"❌ Traceback: {traceback.format_exc()}")
            return {
                'eligible': True,
                'blocked': False,
                'reason': 'Eligibility check bypassed due to error',
                'message': 'Learn & Earn available - take the quiz to earn instant G$ rewards!',
                'feature_available': True,
                'max_reward': 0,
                'can_take_now': True,
                'cooldown_hours': 120,
                'error': str(e)
            }

    def get_quiz_history(self, wallet_address, limit=500):
        """Get user's quiz history - OPTIMIZED single query with OR filter"""
        try:
            supabase = get_supabase_client()

            wallet_normalized = wallet_address.lower()
            masked_address = self.mask_wallet_address(wallet_address)

            logger.info(f"🔍 Optimized quiz history fetch for: {wallet_address[:10]}...")

            result = supabase.table('learnearn_log')\
                .select('*')\
                .or_(f"wallet_address.eq.{masked_address},wallet_address.eq.{wallet_normalized},wallet_address.eq.{wallet_address}")\
                .order('timestamp', desc=True)\
                .limit(limit)\
                .execute()

            seen_quiz_ids = set()
            unique_history = []

            for quiz in (result.data or []):
                quiz_id = quiz.get('quiz_id')
                if quiz_id and quiz_id not in seen_quiz_ids:
                    seen_quiz_ids.add(quiz_id)
                    # Add explorer_url for each quiz
                    quiz_with_url = quiz.copy()
                    if quiz.get('transaction_hash'):
                        quiz_with_url['explorer_url'] = f"https://celoscan.io/tx/{quiz['transaction_hash']}"
                    unique_history.append(quiz_with_url)

            logger.info(f"✅ Found {len(unique_history)} unique quiz history records")

            if unique_history:
                newest_date = unique_history[0].get('timestamp', 'Unknown')
                oldest_date = unique_history[-1].get('timestamp', 'Unknown')
                logger.info(f"📅 Date range: {newest_date} (newest) to {oldest_date} (oldest)")

                # Log summary by month
                from collections import defaultdict
                monthly_counts = defaultdict(int)
                for quiz in unique_history:
                    timestamp = quiz.get('timestamp', '')
                    if timestamp:
                        month = timestamp[:7]  # YYYY-MM
                        monthly_counts[month] += 1

                logger.info(f"📊 Quiz history by month:")
                for month in sorted(monthly_counts.keys()):
                    logger.info(f"   {month}: {monthly_counts[month]} quizzes")

            return unique_history

        except Exception as e:
            logger.error(f"❌ Failed to get quiz history: {e}")
            import traceback
            logger.error(f"🔍 Traceback: {traceback.format_exc()}")
            return []

    def create_quiz_session(self, user_wallet, questions):
        """Creates a new quiz session and stores it in database for production reliability."""
        import json
        session_id = f"QUIZ_SESSION_{user_wallet.lower()}_{random.randint(1000, 9999)}_{int(datetime.utcnow().timestamp())}"

        # Store session data in database for production reliability (works with multiple workers)
        try:
            from supabase_client import get_supabase_client, safe_supabase_operation
            supabase = get_supabase_client()

            if supabase:
                # Delete any old sessions for this user (cleanup)
                safe_supabase_operation(
                    lambda: supabase.table('quiz_sessions').delete().eq('wallet_address', user_wallet.lower()).execute(),
                    fallback_result=None,
                    operation_name="cleanup old quiz sessions"
                )

                # Insert new session
                session_data = {
                    'session_id': session_id,
                    'wallet_address': user_wallet.lower(),
                    'questions': json.dumps(questions),
                    'started_at': datetime.utcnow().isoformat() + 'Z',
                    'expires_at': (datetime.utcnow() + timedelta(hours=1)).isoformat() + 'Z'
                }

                result = safe_supabase_operation(
                    lambda: supabase.table('quiz_sessions').insert(session_data).execute(),
                    fallback_result=None,
                    operation_name="create quiz session in database"
                )

                if result:
                    logger.info(f"✅ Created quiz session in database: {session_id} for {user_wallet}")
                else:
                    logger.warning(f"⚠️ Failed to store session in database, using in-memory fallback")
                    # Fallback to in-memory
                    if not hasattr(self, '_quiz_sessions'):
                        self._quiz_sessions = {}
                    self._quiz_sessions[session_id] = {
                        'questions': questions,
                        'wallet': user_wallet,
                        'started_at': datetime.utcnow().isoformat() + 'Z'
                    }
            else:
                # Fallback to in-memory if no database
                if not hasattr(self, '_quiz_sessions'):
                    self._quiz_sessions = {}
                self._quiz_sessions[session_id] = {
                    'questions': questions,
                    'wallet': user_wallet,
                    'started_at': datetime.utcnow().isoformat() + 'Z'
                }
                logger.info(f"✅ Created quiz session in memory: {session_id} for {user_wallet}")
        except Exception as e:
            logger.error(f"❌ Error creating quiz session: {e}")
            # Fallback to in-memory
            if not hasattr(self, '_quiz_sessions'):
                self._quiz_sessions = {}
            self._quiz_sessions[session_id] = {
                'questions': questions,
                'wallet': user_wallet,
                'started_at': datetime.utcnow().isoformat() + 'Z'
            }

        return {'session_id': session_id}

    def validate_and_score_quiz(self, quiz_session_id, user_answers):
        """Validates quiz session, scores answers, and returns the result."""
        import json

        if not hasattr(self, '_quiz_sessions'):
            self._quiz_sessions = {}

        quiz_session_id = str(quiz_session_id)
        session_data = self._quiz_sessions.get(quiz_session_id)

        # If not in memory, try to get from database (production reliability)
        if not session_data:
            logger.info(f"🔍 Session not in memory, checking database for: {quiz_session_id}")
            try:
                from supabase_client import get_supabase_client, safe_supabase_operation
                supabase = get_supabase_client()

                if supabase:
                    result = safe_supabase_operation(
                        lambda: supabase.table('quiz_sessions').select('*').eq('session_id', quiz_session_id).execute(),
                        fallback_result=None,
                        operation_name="get quiz session from database"
                    )

                    if result and result.data and len(result.data) > 0:
                        db_session = result.data[0]
                        session_data = {
                            'questions': json.loads(db_session['questions']),
                            'wallet': db_session['wallet_address'],
                            'started_at': db_session['started_at']
                        }
                        logger.info(f"✅ Retrieved session from database: {quiz_session_id}")

                        # Clean up from database after retrieval
                        safe_supabase_operation(
                            lambda: supabase.table('quiz_sessions').delete().eq('session_id', quiz_session_id).execute(),
                            fallback_result=None,
                            operation_name="cleanup quiz session from database"
                        )
            except Exception as e:
                logger.error(f"❌ Error retrieving session from database: {e}")

        if not session_data:
            logger.error(f"❌ Session {quiz_session_id} not found in memory or database")
            return {'valid': False, 'message': 'Quiz session expired or not found. Please start a new quiz.'}

        stored_questions = session_data.get('questions')

        if not stored_questions:
            return {'valid': False, 'message': 'Quiz questions not found in session.'}

        if len(user_answers) != len(stored_questions):
            return {'valid': False, 'message': f'Incorrect number of answers submitted. Expected {len(stored_questions)}, received {len(user_answers)}.'}

        correct_answers = 0
        answer_details = []

        for i, question in enumerate(stored_questions):
            user_answer = user_answers[i]
            is_correct = user_answer == question['correct_answer']
            if is_correct:
                correct_answers += 1
            answer_details.append({
                'question_number': i + 1,
                'question_id': question['question_id'],
                'user_answer': user_answer,
                'correct_answer': question['correct_answer'],
                'is_correct': is_correct,
                'category': question.get('category', 'general')
            })

        score = correct_answers
        total_questions = len(stored_questions)
        raw_reward = correct_answers * self.reward_per_correct
        reward_amount = round(min(raw_reward, self.max_reward_per_quiz), 2)

        # Clean up session after scoring
        if quiz_session_id in self._quiz_sessions:
            del self._quiz_sessions[quiz_session_id]

        return {
            'valid': True,
            'score': score,
            'total_questions': total_questions,
            'correct_answers': correct_answers,
            'reward_amount': reward_amount,
            'answers_details': answer_details,
            'questions': stored_questions  # Include questions for further processing
        }

    def log_quiz_attempt(self, user_wallet, score, total_questions, reward_amount, quiz_session_id):
        """Logs a quiz attempt to Supabase."""
        try:
            supabase = get_supabase_client()
            log_id = f"LOG_{user_wallet.lower()}_{datetime.utcnow().isoformat()}"

            quiz_log_data = {
                'quiz_id': log_id,
                'wallet_address': self.mask_wallet_address(user_wallet),
                'timestamp': datetime.utcnow().isoformat() + 'Z', # Use UTC with Z suffix
                'score': score,
                'total_questions': total_questions,
                'amount_g$': reward_amount,
                'status': True, # Assuming successful logging for now, status updated on reward/failure
                'answers': json.dumps([]), # Placeholder, actual answers might be stored elsewhere or not needed for log
                'ubi_verification': json.dumps({}), # Placeholder
                'blocked': False, # Placeholder
                'session_id': quiz_session_id # Link to the session
            }

            result = supabase.table('learnearn_log').insert(quiz_log_data).execute()
            logger.info(f"✅ Quiz attempt logged: {log_id} for {user_wallet}")
            return {'success': True, 'log_id': log_id}

        except Exception as e:
            logger.error(f"❌ Error logging quiz attempt: {e}")
            return {'success': False, 'error': str(e)}

    def update_quiz_log_with_transaction(self, log_id, transaction_hash):
        """Updates the quiz log with transaction details."""
        try:
            supabase = get_supabase_client()
            update_result = supabase.table('learnearn_log')\
                .update({
                    'transaction_hash': transaction_hash,
                    'reward_status': 'sent',
                    'sent_at': datetime.utcnow().isoformat() + 'Z' # Use UTC with Z suffix
                })\
                .eq('quiz_id', log_id)\
                .execute()
            logger.info(f"✅ Updated quiz log {log_id} with transaction hash: {transaction_hash}")
            return True
        except Exception as e:
            logger.error(f"❌ Error updating quiz log {log_id} with transaction: {e}")
            return False

    def get_module_links(self):
        """Get active module links for Learn & Earn with automatic content scraping"""
        try:
            from supabase_client import get_supabase_client
            supabase = get_supabase_client()

            if not supabase:
                logger.error("❌ Supabase client not available for module links")
                return []

            # Get active module links
            result = supabase.table('learn_earn_module_links')\
                .select('id, title, url, description, content, reading_time_minutes, display_order')\
                .eq('is_active', True)\
                .order('display_order', desc=False)\
                .execute()

            if result.data and len(result.data) > 0:
                logger.info(f"✅ Retrieved {len(result.data)} module links")

                # Auto-scrape missing content
                for link in result.data:
                    has_content = bool(link.get('content') and link.get('content').strip())
                    has_url = bool(link.get('url') and link.get('url').strip())

                    # If no content but has URL, auto-scrape
                    if not has_content and has_url:
                        logger.info(f"🔍 Auto-scraping content for '{link.get('title')}' from {link.get('url')}")
                        try:
                            import requests
                            from bs4 import BeautifulSoup

                            # Fetch webpage
                            response = requests.get(link.get('url'), timeout=10, headers={
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                            })
                            response.raise_for_status()

                            # Parse HTML
                            soup = BeautifulSoup(response.content, 'html.parser')

                            # Remove unwanted elements
                            for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside']):
                                element.decompose()

                            # Extract main content
                            main_content = (
                                soup.find('article') or
                                soup.find('main') or
                                soup.find('div', class_='content') or
                                soup.find('div', class_='article') or
                                soup.find('body')
                            )

                            if main_content:
                                scraped_html = ""

                                # Extract headings and paragraphs
                                for element in main_content.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol']):
                                    if element.name == 'h1':
                                        scraped_html += f"<h2>{element.get_text().strip()}</h2>\n"
                                    elif element.name == 'h2':
                                        scraped_html += f"<h3>{element.get_text().strip()}</h3>\n"
                                    elif element.name == 'h3':
                                        scraped_html += f"<h3>{element.get_text().strip()}</h3>\n"
                                    elif element.name == 'p':
                                        text = element.get_text().strip()
                                        if text:
                                            scraped_html += f"<p>{text}</p>\n"
                                    elif element.name == 'ul':
                                        scraped_html += "<ul>\n"
                                        for li in element.find_all('li', recursive=False):
                                            scraped_html += f"<li>{li.get_text().strip()}</li>\n"
                                        scraped_html += "</ul>\n"
                                    elif element.name == 'ol':
                                        scraped_html += "<ol>\n"
                                        for li in element.find_all('li', recursive=False):
                                            scraped_html += f"<li>{li.get_text().strip()}</li>\n"
                                        scraped_html += "</ol>\n"

                                content = scraped_html.strip()

                                # Auto-calculate reading time
                                word_count = len(content.split())
                                reading_time = max(1, round(word_count / 200))

                                # Update database with scraped content
                                supabase.table('learn_earn_module_links')\
                                    .update({
                                        'content': content,
                                        'reading_time_minutes': reading_time
                                    })\
                                    .eq('id', link.get('id'))\
                                    .execute()

                                # Update in-memory link data
                                link['content'] = content
                                link['reading_time_minutes'] = reading_time

                                logger.info(f"✅ Auto-scraped {len(content)} chars, {word_count} words, ~{reading_time} min read")
                            else:
                                logger.warning(f"⚠️ Could not find main content in {link.get('url')}")

                        except Exception as scrape_error:
                            logger.error(f"❌ Auto-scrape failed for '{link.get('title')}': {scrape_error}")

                    # Log final status
                    has_content = bool(link.get('content') and link.get('content').strip())
                    logger.info(f"   Module '{link.get('title')}': has_content={has_content}, reading_time={link.get('reading_time_minutes')}min")

                return result.data
            else:
                logger.warning("⚠️ No active module links found in database")
                return []

        except Exception as e:
            logger.error(f"❌ Error getting module links: {e}")
            import traceback
            logger.error(f"❌ Traceback: {traceback.format_exc()}")
            return []

    def get_username_from_db(self, wallet_address: str):
        """Get username for wallet address from user_data table"""
        try:
            supabase = get_supabase_client()
            if not supabase:
                return None

            # Query user_data table for username
            result = supabase.table("user_data")\
                .select("username")\
                .eq("wallet_address", wallet_address)\
                .execute()

            if result.data and len(result.data) > 0:
                username = result.data[0].get("username")
                if username and username.strip():
                    logger.info(f"✅ Retrieved username from user_data: {username} for {wallet_address[:8]}...")
                    return username.strip()

            logger.info(f"ℹ️ No username found in user_data for {wallet_address[:8]}...")
            return None

        except Exception as e:
            logger.error(f"❌ Error getting username from user_data: {e}")
            return None

    def get_daily_ranking(self, wallet_address: str, quiz_date: str = None):
        """Get user's ranking for a specific date from database"""
        try:
            from datetime import datetime
            supabase = get_supabase_client()
            if not supabase:
                return {'position': 1, 'badge': '🎯 PARTICIPANT'}

            # Use provided date or today
            if not quiz_date:
                quiz_date = datetime.utcnow().strftime('%Y-%m-%d')

            # Query all quizzes for this date, ordered by timestamp (earliest first)
            start_datetime = f"{quiz_date}T00:00:00Z"
            end_datetime = f"{quiz_date}T23:59:59Z"

            logger.info(f"📊 Getting daily ranking for {wallet_address[:8]}... on {quiz_date}")

            result = supabase.table('learnearn_log')\
                .select('wallet_address, timestamp, quiz_id')\
                .gte('timestamp', start_datetime)\
                .lte('timestamp', end_datetime)\
                .eq('status', True)\
                .order('timestamp', desc=False)\
                .execute()

            if not result.data:
                # First participant of the day
                return {'position': 1, 'badge': '🥇 FIRST PLACE'}

            # Find user's position
            masked_address = self.mask_wallet_address(wallet_address)
            position = 0

            for idx, quiz in enumerate(result.data, start=1):
                if quiz['wallet_address'] == masked_address:
                    position = idx
                    break

            # If user not found, they will be next participant
            if position == 0:
                position = len(result.data) + 1

            # Determine badge
            if position == 1:
                badge = '🥇 FIRST PLACE'
            elif position == 2:
                badge = '🥈 SECOND PLACE'
            elif position == 3:
                badge = '🥉 THIRD PLACE'
            elif position <= 10:
                badge = f'⭐ TOP {position}'
            else:
                badge = f'🎯 RANK #{position}'

            logger.info(f"✅ User ranking: Position {position}, Badge: {badge}")

            return {'position': position, 'badge': badge}

        except Exception as e:
            logger.error(f"❌ Error getting daily ranking: {e}")
            return {'position': 1, 'badge': '🎯 PARTICIPANT'}


# Initialize Quiz Manager
quiz_manager = LearnEarnQuizManager()

def learn_earn_token_required(f):
    """Token validation for Learn & Earn endpoints"""
    @wraps(f)
    def decorated(*args, **kwargs):
        wallet_address = session.get('wallet')
        verified = session.get('verified')

        logger.info(f"🔐 Learn & Earn Auth Check: wallet={wallet_address is not None}, verified={verified}")

        if not wallet_address:
            logger.warning("❌ No wallet address in session")
            return jsonify({
                'success': False,
                'error': 'No wallet address found. Please log in again.',
                'auth_required': True
            }), 401

        if not verified:
            logger.warning("❌ User not verified")
            return jsonify({
                'success': False,
                'error': 'User not verified. Please complete UBI verification first.',
                'verification_required': True
            }), 401

        logger.info(f"✅ Authentication successful for {wallet_address}")
        return f(wallet_address, *args, **kwargs)
    return decorated

# Learn & Earn Routes

@learn_earn_bp.route('/')
def learn_earn_dashboard():
    """Learn & Earn main dashboard"""
    from flask import render_template, session, redirect, url_for

    # Check if user is authenticated
    if not session.get('verified') or not session.get('wallet'):
        return redirect(url_for('routes.index'))

    wallet = session.get('wallet')
    is_admin_user = False
    try:
        from supabase_client import is_admin
        is_admin_user = bool(is_admin(wallet))
    except Exception as admin_err:
        logger.warning(f"⚠️ Could not resolve admin status for Learn & Earn dashboard: {admin_err}")

    # Track page visit
    try:
        from analytics_service import analytics
        analytics.track_page_view(wallet, 'learn_earn')
    except Exception as e:
        logger.error(f"❌ Error tracking analytics: {e}")

    from flask import make_response
    from .nft_service import achievement_nft_service
    resp = make_response(render_template(
        'learn_and_earn.html',
        wallet=wallet,
        login_method=session.get('login_method', 'walletconnect'),
        is_admin_user=is_admin_user,
        escrow_address=achievement_nft_service.escrow_address,
        escrow_enabled=achievement_nft_service.is_escrow_configured
    ))
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    resp.headers['Expires'] = '0'
    return resp

@learn_earn_bp.route('/start-quiz', methods=['POST'])
@learn_earn_token_required
def start_quiz(current_user):
    """Start a new Learn & Earn quiz"""
    try:
        logger.info(f"🎯 Starting quiz for user: {current_user}")

        # NOTE: Session clearing moved to AFTER validation passes
        # This prevents losing valid quiz session if validation fails

        # Check if reward system is configured (safe check without exposing private key)
        if not learn_blockchain_service.is_configured:
            logger.error(f"❌ Reward system not configured")
            return jsonify({
                'success': False,
                'error': 'Learn wallet not configured',
                'show_notification': True,
                'notification_type': 'wallet_not_configured',
                'notification_message': 'Contact GIMT team to remind them to refill the token rewards',
                'voice_message': 'Contact GIMT team to remind them to refill the token rewards',
                'feature_status': 'wallet_not_configured'
            }), 400

        # Check Learn wallet balance before allowing quiz
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            learn_balance = loop.run_until_complete(learn_blockchain_service.get_learn_wallet_balance())
        finally:
            loop.close()

        # Check if Learn wallet has sufficient balance (at least 2000 G$ for full quiz rewards)
        min_required_balance = quiz_manager.questions_per_quiz * quiz_manager.reward_per_correct
        # Use tolerance to avoid floating point precision issues (e.g., 1000.0 vs 1000.0000000000001)
        balance_tolerance = 0.01  # Allow 0.01 G$ tolerance
        if learn_balance < (min_required_balance - balance_tolerance):
            logger.warning(f"⚠️ Learn wallet balance too low: {learn_balance} < {min_required_balance}")

            # Get custom message from database
            from supabase_client import get_supabase_client, safe_supabase_operation
            supabase = get_supabase_client()
            custom_message = 'G$ funds have been depleted. Please try to contact us at t.me/GoodDollarX'

            if supabase:
                msg_result = safe_supabase_operation(
                    lambda: supabase.table('maintenance_settings')\
                        .select('custom_message')\
                        .eq('feature_name', 'learn_earn_insufficient_balance')\
                        .execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="get insufficient balance custom message"
                )
                if msg_result.data and len(msg_result.data) > 0:
                    db_message = msg_result.data[0].get('custom_message')
                    if db_message:
                        custom_message = db_message

            return jsonify({
                'success': False,
                'error': custom_message,
                'show_notification': True,
                'notification_type': 'insufficient_balance',
                'notification_message': custom_message,
                'voice_message': 'G$ funds depleted. Contact us at t.me/GoodDollarX',
                'learn_balance': learn_balance,
                'required_balance': min_required_balance,
                'feature_status': 'insufficient_balance'
            }), 400

        # Check user eligibility using sync method like hour_bonus
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            eligibility_info = loop.run_until_complete(quiz_manager.check_quiz_eligibility(current_user))
        finally:
            loop.close()

        if eligibility_info.get('blocked'):
            return jsonify({
                'success': False,
                'blocked': True,
                'reason': eligibility_info.get('reason'),
                'message': eligibility_info.get('message'),
                'next_quiz_time': eligibility_info.get('next_quiz_time'),
                'can_take_now': eligibility_info.get('can_take_now', False),
                'feature_status': 'cooldown_active'
            }), 403

        # Get random questions using sync method like hour_bonus
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            questions = loop.run_until_complete(quiz_manager.get_random_questions(quiz_manager.questions_per_quiz))
        finally:
            loop.close()

        if not questions:
            return jsonify({
                'success': False,
                'error': 'No questions available. Please try again later.'
            }), 500

        # Get module links BEFORE creating quiz session
        module_links = quiz_manager.get_module_links()
        logger.info(f"📚 Retrieved {len(module_links)} module links for quiz session")

        # Filter out module links without content
        valid_module_links = []
        if module_links:
            for idx, link in enumerate(module_links):
                content = link.get('content')

                # Skip if content is None or empty
                if content is None or content == '':
                    logger.warning(f"   ⚠️ Module {idx + 1}: '{link.get('title')}' - content is NULL or empty in database (SKIPPED)")
                    continue

                # Convert to string and check if it has actual content
                content_str = str(content).strip()
                if not content_str:
                    logger.warning(f"   ⚠️ Module {idx + 1}: '{link.get('title')}' - content is whitespace only (SKIPPED)")
                    continue

                # Valid content found
                logger.info(f"   ✅ Module {idx + 1}: '{link.get('title')}' - valid content ({len(content_str)} chars, {link.get('reading_time_minutes', 5)} min reading time)")
                valid_module_links.append(link)

        logger.info(f"📚 {len(valid_module_links)} module links with valid content will be shown")

        # Clear any existing quiz session data ONLY after all validations pass
        # This prevents losing a valid quiz session if validation fails
        session.pop('quiz_questions', None)
        session.pop('quiz_session_id', None)
        session.pop('quiz_started_at', None)
        logger.info(f"🧹 Cleared previous quiz session data for {current_user}")

        # Create quiz session
        quiz_session = quiz_manager.create_quiz_session(current_user, questions)

        logger.info(f"📝 Created quiz session: {quiz_session['session_id']}")

        # Store questions in session for submit-quiz endpoint
        session['quiz_questions'] = questions
        session['quiz_session_id'] = quiz_session['session_id']
        session['quiz_started_at'] = datetime.utcnow().isoformat() + 'Z'

        # Make session permanent FIRST before setting data
        session.permanent = True
        session.modified = True  # Force session to save

        # Verify session data was stored correctly
        logger.info(f"✅ Session data stored - Questions: {len(session.get('quiz_questions', []))}, Session ID: {session.get('quiz_session_id')}")
        logger.info(f"🔒 Session permanent: {session.permanent}, Modified: {session.modified}")

        # Prepare quiz questions for response (remove correct answers)
        quiz_questions_for_response = []
        for q in questions:
            quiz_questions_for_response.append({
                'question_number': q['question_number'],
                'question_id': q['question_id'],
                'question': q['question'],
                'options': q['options'],
                'category': q.get('category'),
                'difficulty': q.get('difficulty')
            })

        return jsonify({
            'success': True,
            'quiz_session': {
                'session_id': quiz_session['session_id'],
                'started_at': session['quiz_started_at'],
                'questions': quiz_questions_for_response,
                'quiz_info': {
                    'total_questions': len(quiz_questions_for_response),
                    'time_per_question': quiz_manager.time_per_question,
                    'reward_per_correct': quiz_manager.reward_per_correct,
                    'max_reward': len(quiz_questions_for_response) * quiz_manager.reward_per_correct
                },
                'module_links': valid_module_links
            },
            'feature_status': 'available',
            'learn_balance': learn_balance
        }), 200

    except Exception as e:
        logger.error(f"❌ Error starting quiz for {current_user}: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to start quiz. Please try again.'
        }), 500

@learn_earn_bp.route('/submit-quiz', methods=['POST'])
@learn_earn_token_required
def submit_quiz(current_user):
    """Submit quiz answers and process rewards"""
    try:
        data = request.get_json()
        user_answers = data.get('answers', [])
        quiz_session_id = data.get('quiz_session_id')

        logger.info(f"📝 Quiz submission from {current_user} for session {quiz_session_id}")

        # First, try to retrieve from session
        stored_questions = session.get('quiz_questions')
        stored_session_id = session.get('quiz_session_id')

        logger.info(f"📋 Submit Quiz Debug - Current User: {current_user}")
        logger.info(f"📋 Session ID from request: {quiz_session_id}")
        logger.info(f"📋 Session ID from session: {stored_session_id}")
        logger.info(f"✅ Session has questions: {stored_questions is not None}")
        logger.info(f"✅ Session ID match: {stored_session_id == quiz_session_id}")
        logger.info(f"📊 Questions count: {len(stored_questions) if stored_questions else 0}")

        # If session data is missing, try to retrieve from quiz_manager's temporary storage
        if not stored_questions or str(stored_session_id) != str(quiz_session_id):
            logger.warning(f"⚠️ Session data missing or mismatch (Session: {stored_session_id}, Request: {quiz_session_id}), trying quiz_manager storage...")

            # Check if quiz_manager has the session in memory
            quiz_session_id_str = str(quiz_session_id)
            if hasattr(quiz_manager, '_quiz_sessions') and quiz_session_id_str in quiz_manager._quiz_sessions:
                session_data = quiz_manager._quiz_sessions[quiz_session_id_str]
                stored_questions = session_data.get('questions')
                logger.info(f"✅ Retrieved questions from quiz_manager for session {quiz_session_id_str}")
            elif stored_questions and session.get('wallet') == current_user:
                logger.info(f"💡 Found questions in session for user {current_user}, proceeding despite ID mismatch")
            else:
                # Not in memory — allow validate_and_score_quiz to attempt the database fallback
                logger.warning(f"⚠️ Session not in Flask session or memory for {current_user}, will attempt database lookup in validate_and_score_quiz")

        # Validate and score quiz
        quiz_result = quiz_manager.validate_and_score_quiz(quiz_session_id, user_answers)

        if not quiz_result.get('valid'):
            return jsonify({
                'success': False,
                'message': quiz_result.get('message', 'Invalid quiz submission')
            }), 400

        score = quiz_result['score']
        total_questions = quiz_result['total_questions']
        reward_amount = quiz_result['reward_amount']

        logger.info(f"📊 Quiz results: {score}/{total_questions} correct, {reward_amount} G$ earned")

        # Create feature status info for logging
        # Check eligibility for logging purposes, though submission is allowed regardless
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            eligibility_info_for_log = loop.run_until_complete(quiz_manager.check_quiz_eligibility(current_user))
        finally:
            loop.close()

        feature_info_for_log = {
            'feature_available': True,
            'access_granted': True,
            'blocked': eligibility_info_for_log.get('blocked', False),
            'feature_type': 'learn_and_earn',
            'submission_allowed': True # Submission is always allowed, reward is conditional
        }

        # Prefer validated questions from scoring path to avoid missing-session save failures
        questions_for_log = quiz_result.get('questions') or stored_questions or []

        # Process G$ reward disbursement only when there is a positive reward.
        # Failed quiz attempts can validly earn 0 G$, so we should still record completion
        # without attempting an on-chain transfer. Streaming mode queues a row that the
        # in-process scheduler (or the /process-streams admin endpoint) will pick up and
        # settle on-chain via Superfluid CFA. If the streaming runtime is not configured
        # (missing Superfluid env vars / SuperToken address / wallet key), or the queue
        # insert fails for any reason, we fall back to the legacy instant transfer so the
        # quiz submit path never hard-fails the user — they always get paid one way or the
        # other.
        disbursement_result = None
        tx_hash = None
        stream_job = None
        effective_mode = 'instant'
        payout_mode = STREAMING_PAYOUT_MODE

        if reward_amount > 0 and _is_streaming_mode(payout_mode):
            ready, reason = streaming_service.is_runtime_ready()
            if not ready:
                logger.warning(
                    f"⚠️ Streaming mode requested but runtime not ready ({reason}); "
                    f"falling back to instant reward for {current_user[:8]}..."
                )
            else:
                stream_job = streaming_service.create_stream_job(
                    current_user,
                    reward_amount,
                    reward_id=f"quiz:{quiz_session_id}"
                )
                if stream_job.get('success'):
                    effective_mode = 'stream'
                    tx_hash = f"queued:{stream_job['stream'].get('id', '')}"
                else:
                    err = stream_job.get('error', 'Failed to queue streaming reward')
                    logger.error(
                        f"❌ Stream queue failed for {current_user[:8]}...: {err}; "
                        "falling back to instant reward."
                    )

        if reward_amount > 0 and effective_mode == 'instant':
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                disbursement_result = loop.run_until_complete(
                    learn_blockchain_service.send_g_reward(
                        current_user,
                        reward_amount,
                        {
                            'action': 'quiz_submit',
                            'quiz_session_id': quiz_session_id,
                            'score': score,
                            'total': total_questions
                        }
                    )
                )
            finally:
                loop.close()

            if not disbursement_result or not disbursement_result.get('success'):
                err = (disbursement_result or {}).get('error', 'Failed to process instant quiz reward')
                logger.error(f"❌ Instant reward disbursement failed for {current_user[:8]}...: {err}")
                return jsonify({
                    'success': False,
                    'error': err,
                    'message': 'Quiz submitted but reward transfer failed. Please try again later.'
                }), 500

            tx_hash = disbursement_result.get('tx_hash')
        elif reward_amount <= 0:
            logger.info(
                f"ℹ️ Quiz completed with 0 reward for {current_user[:8]}...; "
                "skipping blockchain disbursement."
            )

        # Save quiz attempt to database
        quiz_log = None
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            quiz_log = loop.run_until_complete(quiz_manager.save_quiz_attempt(
                current_user,
                questions_for_log,
                user_answers,
                reward_amount,
                feature_info_for_log
            ))
        finally:
            loop.close()

        if quiz_log and tx_hash:
            quiz_manager.update_quiz_log_with_transaction(quiz_log.get('quiz_id'), tx_hash)

        # Clear quiz session
        session.pop('quiz_questions', None)
        session.pop('quiz_session_id', None)
        session.pop('quiz_started_at', None)

        # Invalidate quiz history cache so next fetch shows the new attempt
        _quiz_history_cache.pop(current_user, None)

        if tx_hash:
            label = 'Stream queued' if effective_mode == 'stream' else 'Instant reward sent'
            logger.info(
                f"✅ Quiz completed for {current_user} — {score}/{total_questions}. "
                f"{label}: {reward_amount} G$ tx={tx_hash}"
            )
        else:
            logger.info(
                f"✅ Quiz completed for {current_user} — {score}/{total_questions}. "
                f"No reward disbursed (earned: {reward_amount} G$)."
            )

        saved_quiz_id = quiz_log.get('quiz_id', '') if quiz_log else ''

        return jsonify({
            'success': True,
            'score': score,
            'total_questions': total_questions,
            'rewards': reward_amount,
            'quiz_id': saved_quiz_id,
            'message': (
                (
                    f'Quiz completed! You earned {reward_amount} G$ via 1-day stream payout.'
                    if effective_mode == 'stream'
                    else f'Quiz completed! You earned {reward_amount} G$ instantly.'
                )
                if reward_amount > 0
                else f'Quiz completed! You earned {reward_amount} G$ this round.'
            ),
            'transaction_hash': tx_hash,
            'explorer_url': f'https://celoscan.io/tx/{tx_hash}' if tx_hash and not str(tx_hash).startswith('queued:') else '',
            'payout_mode': effective_mode,
            'requested_payout_mode': payout_mode,
            'feature_status': 'completed_successfully',
            'blocked_for_24h': True,
            'can_retry_immediately': False,
            'show_notification': True,
            'notification_type': 'success',
            'notification_message': (
                (
                    f'Quiz completed! {score}/{total_questions} correct. Stream payout queued: {reward_amount} G$ over 1 day.'
                    if effective_mode == 'stream'
                    else f'Quiz completed! {score}/{total_questions} correct. Reward sent instantly: {reward_amount} G$.'
                )
                if reward_amount > 0
                else f'Quiz completed! {score}/{total_questions} correct. No reward was sent for this attempt.'
            )
        }), 200

    except Exception as e:
        logger.error(f"❌ Error submitting quiz for {current_user}: {e}")
        # Clear session on error to prevent retries with stale data
        session.pop('quiz_questions', None)
        session.pop('quiz_session_id', None)
        session.pop('quiz_started_at', None)
        return jsonify({
            'success': False,
            'error': 'Failed to submit quiz. Please try again.'
        }), 500

@learn_earn_bp.route('/get-daily-ranking', methods=['GET'])
@learn_earn_token_required
def get_daily_ranking(current_user):
    """Get user's ranking for a specific date"""
    try:
        from datetime import datetime
        from flask import request

        quiz_date = request.args.get('date', datetime.utcnow().strftime('%Y-%m-%d'))

        # Get all quizzes for this date, ordered by timestamp (earliest first)
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        # Get all participants for this date
        start_datetime = f"{quiz_date}T00:00:00Z"
        end_datetime = f"{quiz_date}T23:59:59Z"

        result = supabase.table('learnearn_log')\
            .select('wallet_address, timestamp, quiz_id, score, total_questions')\
            .gte('timestamp', start_datetime)\
            .lte('timestamp', end_datetime)\
            .eq('status', True)\
            .order('timestamp', desc=False)\
            .execute()

        if not result.data:
            # First participant of the day
            return jsonify({
                'success': True,
                'rank': 1,
                'badge': '🥇 FIRST PLACE',
                'total_participants': 1,
                'date': quiz_date,
                'is_first': True
            })

        # Find user's rank (based on who took quiz first)
        masked_address = quiz_manager.mask_wallet_address(current_user)
        user_rank = 0

        # Check if user already has a quiz today
        for idx, quiz in enumerate(result.data, start=1):
            if quiz['wallet_address'] == masked_address:
                user_rank = idx
                break

        # If user not found in results, they will be the next participant
        if user_rank == 0:
            user_rank = len(result.data) + 1

        # Determine badge
        badge = ''
        if user_rank == 1:
            badge = '🥇 FIRST PLACE'
        elif user_rank == 2:
            badge = '🥈 SECOND PLACE'
        elif user_rank == 3:
            badge = '🥉 THIRD PLACE'
        elif user_rank <= 10:
            badge = f'⭐ TOP {user_rank}'
        else:
            badge = f'🎯 RANK #{user_rank}'

        return jsonify({
            'success': True,
            'rank': user_rank,
            'badge': badge,
            'total_participants': len(result.data) if user_rank <= len(result.data) else user_rank,
            'date': quiz_date
        })

    except Exception as e:
        logger.error(f"❌ Error getting daily ranking: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@learn_earn_bp.route('/eligibility', methods=['GET'])
@learn_earn_token_required
def check_eligibility(current_user):
    """Check user eligibility for Learn & Earn quiz"""
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            eligibility_info = loop.run_until_complete(quiz_manager.check_quiz_eligibility(current_user))
        except Exception as async_error:
            logger.error(f"❌ Async eligibility check error: {async_error}")
            # Return a safe default response
            eligibility_info = {
                'eligible': True,
                'blocked': False,
                'reason': 'Check bypassed due to error',
                'message': 'Quiz is available - you can take it now!',
                'can_take_now': True,
                'feature_available': True,
                'error': str(async_error)
            }
        finally:
            loop.close()

        return jsonify({
            'success': True,
            'eligible': eligibility_info.get('eligible', False),
            'blocked': eligibility_info.get('blocked', False),
            'reason': eligibility_info.get('reason'),
            'message': eligibility_info.get('message'),
            'next_quiz_time': eligibility_info.get('next_quiz_time'),
            'can_take_now': eligibility_info.get('can_take_now', False),
            'ubi_verification': eligibility_info # Contains all info, including cooldown details
        }), 200

    except Exception as e:
        logger.error(f"❌ Error checking eligibility for {current_user}: {e}")
        import traceback
        logger.error(f"❌ Traceback: {traceback.format_exc()}")
        # Return a safe fallback response instead of 500 error
        return jsonify({
            'success': True,
            'eligible': True,
            'blocked': False,
            'reason': 'Check bypassed due to error',
            'message': 'Quiz is available - you can take it now!',
            'can_take_now': True,
            'error': str(e)
        }), 200

def get_sell_start_date():
    """Fetch the achievement card sell start date from DB, fallback to May 10 2026"""
    from datetime import datetime
    try:
        supabase = get_supabase_client()
        if supabase:
            result = supabase.table('maintenance_settings')\
                .select('custom_message')\
                .eq('feature_name', 'learn_earn_sell_date')\
                .execute()
            if result.data and len(result.data) > 0:
                date_str = result.data[0].get('custom_message')
                if date_str:
                    return datetime.strptime(date_str, '%Y-%m-%d')
    except Exception as e:
        logger.warning(f"⚠️ Could not fetch sell date from DB, using default: {e}")
    return datetime(2026, 5, 10, 0, 0, 0)

@learn_earn_bp.route('/sell-achievement-card', methods=['POST'])
@learn_earn_token_required
def sell_achievement_card(current_user):
    """Sell achievement card for G$ based on quiz score"""
    try:
        data = request.get_json()
        quiz_id = data.get('quiz_id')
        score = int(data.get('score', 0))
        total_questions = int(data.get('total_questions', 10))
        original_reward = float(data.get('original_reward', 0))
        sell_price = int(data.get('sell_price', 0))
        quiz_timestamp = data.get('quiz_timestamp')

        logger.info(f"💰 Achievement card sale request from {current_user}")
        logger.info(f"📊 Quiz ID: {quiz_id}, Score: {score}/{total_questions}, Sell Price: {sell_price} G$")

        # Check if selling is available (date managed via admin dashboard)
        from datetime import datetime
        selling_start_date = get_sell_start_date()
        current_date = datetime.utcnow()

        if current_date < selling_start_date:
            days_until_available = (selling_start_date - current_date).days
            logger.warning(f"⚠️ Achievement card selling not yet available. Available on: {selling_start_date.strftime('%B %d, %Y')}")
            return jsonify({
                'success': False,
                'error': f'Achievement card selling will be available on {selling_start_date.strftime("%B %d, %Y")}',
                'selling_available': False,
                'available_date': selling_start_date.strftime('%B %d, %Y'),
                'days_until_available': days_until_available
            }), 403

        # Validate sell price calculation (max 500 G$ for perfect score)
        expected_price = round((score / total_questions) * 500)
        if sell_price != expected_price:
            logger.error(f"❌ Invalid sell price: {sell_price} != {expected_price}")
            return jsonify({
                'success': False,
                'error': 'Invalid sell price calculation'
            }), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({
                'success': False,
                'error': 'Database not available'
            }), 500

        # Check if THIS SPECIFIC card was already sold (by quiz_id)
        existing_sale = supabase.table('achievement_card_sales')\
            .select('*')\
            .eq('wallet_address', current_user)\
            .eq('quiz_id', quiz_id)\
            .execute()

        if existing_sale.data and len(existing_sale.data) > 0:
            logger.warning(f"⚠️ User {current_user[:8]}... already sold this specific card (Quiz ID: {quiz_id})")
            return jsonify({
                'success': False,
                'error': 'This achievement card has already been sold!'
            }), 400

        # Check 1-hour sell cooldown (any card sold in the last 1 hour)
        from datetime import timedelta, timezone
        one_hour_ago = (datetime.utcnow() - timedelta(hours=1)).isoformat() + 'Z'
        recent_sale = supabase.table('achievement_card_sales')\
            .select('created_at')\
            .eq('wallet_address', current_user)\
            .gte('created_at', one_hour_ago)\
            .order('created_at', desc=True)\
            .limit(1)\
            .execute()

        if recent_sale.data and len(recent_sale.data) > 0:
            last_sale_time = datetime.fromisoformat(recent_sale.data[0]['created_at'].replace('Z', '+00:00'))
            next_sell_time = last_sale_time + timedelta(hours=1)
            now_utc = datetime.now(timezone.utc)
            minutes_remaining = int((next_sell_time - now_utc).total_seconds() / 60)
            seconds_remaining = int((next_sell_time - now_utc).total_seconds() % 60)
            logger.warning(f"⚠️ Sell cooldown active for {current_user[:8]}... Next sell in {minutes_remaining}m {seconds_remaining}s")
            return jsonify({
                'success': False,
                'error': f'Please wait {minutes_remaining} minute(s) and {seconds_remaining} second(s) before selling another card.',
                'cooldown_active': True,
                'next_sell_time': next_sell_time.isoformat(),
                'minutes_remaining': minutes_remaining,
                'seconds_remaining': seconds_remaining
            }), 429

        # Process blockchain disbursement
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            disbursement_result = loop.run_until_complete(
                learn_blockchain_service.send_g_reward(
                    current_user,
                    sell_price,
                    {'action': 'sell_achievement_card', 'quiz_id': quiz_id, 'score': score, 'total': total_questions}
                )
            )
        finally:
            loop.close()

        if not disbursement_result.get('success'):
            logger.error(f"❌ Card sale disbursement failed: {disbursement_result.get('error')}")
            return jsonify({
                'success': False,
                'error': disbursement_result.get('error', 'Failed to process card sale')
            }), 500

        # Log card sale to database with quiz_id
        card_sale_data = {
            'wallet_address': current_user,
            'quiz_id': quiz_id,
            'quiz_timestamp': quiz_timestamp,
            'score': score,
            'total_questions': total_questions,
            'original_reward': original_reward,
            'sell_price': sell_price,
            'transaction_hash': disbursement_result.get('tx_hash'),
            'created_at': datetime.utcnow().isoformat() + 'Z'
        }

        supabase.table('achievement_card_sales').insert(card_sale_data).execute()
        for cache_key in list(_card_sales_cache.keys()):
            if cache_key.startswith(f"{current_user}:"):
                _card_sales_cache.pop(cache_key, None)

        logger.info(f"✅ Achievement card sold successfully!")
        logger.info(f"💰 User received: {sell_price} G$")
        logger.info(f"📜 TX: {disbursement_result.get('tx_hash')}")

        # Create notification for the sale
        try:
            from notifications_service import notification_service
            notification_service.create_achievement_sale_notification(
                wallet_address=current_user,
                score=score,
                total_questions=total_questions,
                sell_price=sell_price,
                transaction_hash=disbursement_result.get('tx_hash')
            )
            logger.info(f"✅ Created achievement sale notification for {current_user[:8]}...")
        except Exception as notif_error:
            logger.error(f"⚠️ Failed to create notification: {notif_error}")

        return jsonify({
            'success': True,
            'message': f'Achievement card sold for {sell_price} G$!',
            'sell_price': sell_price,
            'transaction_hash': disbursement_result.get('tx_hash'),
            'explorer_url': f"https://celoscan.io/tx/{disbursement_result.get('tx_hash')}"
        }), 200

    except Exception as e:
        logger.error(f"❌ Error selling achievement card: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': f'Failed to sell achievement card: {str(e)}'
        }), 500

@learn_earn_bp.route('/quiz-history', methods=['GET'])
@learn_earn_token_required
def get_quiz_history_endpoint(current_user):
    """Get user's quiz history"""
    try:
        now = time.time()
        cached = _quiz_history_cache.get(current_user)
        if cached and now < cached[1]:
            logger.info(f"📦 Using cached quiz history for {current_user[:8]}...")
            return jsonify(cached[0])

        limit = int(request.args.get('limit', 500))
        quiz_history = quiz_manager.get_quiz_history(current_user, limit)

        # Calculate total earned
        total_earned = sum(quiz.get('amount_g$', 0) for quiz in quiz_history)

        response = {
            'success': True,
            'quiz_history': quiz_history,
            'total_earned': total_earned,
            'quiz_count': len(quiz_history)
        }
        _quiz_history_cache[current_user] = (response, now + _QUIZ_HISTORY_TTL)
        return jsonify(response)
    except Exception as e:
        logger.error(f"❌ Error getting quiz history: {e}")
        return jsonify({
            'success': False,
            'error': str(e),
            'quiz_history': [],
            'total_earned': 0,
            'quiz_count': 0
        }), 500

@learn_earn_bp.route('/check-card-sold', methods=['POST'])
@learn_earn_token_required
def check_card_sold(current_user):
    """Check if an achievement card was already sold"""
    try:
        data = request.get_json()
        quiz_id = data.get('quiz_id')
        score = data.get('score')
        total_questions = data.get('total_questions')
        timestamp = data.get('timestamp')

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        # Check if selling is available (date managed via admin dashboard)
        from datetime import datetime
        selling_start_date = get_sell_start_date()
        current_date = datetime.utcnow()
        selling_available = current_date >= selling_start_date
        days_until_available = (selling_start_date - current_date).days if not selling_available else 0

        # Check if this specific card was sold using quiz_id
        result = supabase.table('achievement_card_sales')\
            .select('*')\
            .eq('wallet_address', current_user)\
            .eq('quiz_id', quiz_id)\
            .order('created_at', desc=True)\
            .limit(1)\
            .execute()

        if result.data and len(result.data) > 0:
            sale = result.data[0]
            return jsonify({
                'success': True,
                'already_sold': True,
                'sell_price': sale.get('sell_price'),
                'transaction_hash': sale.get('transaction_hash'),
                'sold_at': sale.get('created_at'),
                'selling_available': selling_available,
                'available_date': selling_start_date.strftime('%B %d, %Y'),
                'days_until_available': days_until_available
            })
        else:
            return jsonify({
                'success': True,
                'already_sold': False,
                'selling_available': selling_available,
                'available_date': selling_start_date.strftime('%B %d, %Y'),
                'days_until_available': days_until_available
            })

    except Exception as e:
        logger.error(f"❌ Error checking card sold status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500

@learn_earn_bp.route('/card-sale-history', methods=['GET'])
@learn_earn_token_required
def get_card_sale_history(current_user):
    """Get achievement card sell transaction history for the current user"""
    try:
        limit = int(request.args.get('limit', 50))
        now = time.time()
        cache_key = f"{current_user}:{limit}"
        cached = _card_sales_cache.get(cache_key)
        if cached and now < cached[1]:
            logger.info(f"📦 Using cached card sale history for {current_user[:8]}... (limit={limit})")
            return jsonify(cached[0])

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        result = supabase.table('achievement_card_sales')\
            .select('*')\
            .eq('wallet_address', current_user)\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()

        sales = result.data if result.data else []
        total_earned = sum(float(s.get('sell_price', 0)) for s in sales)

        logger.info(f"📜 Card sale history for {current_user[:8]}...: {len(sales)} records, total {total_earned} G$")

        response = {
            'success': True,
            'sales': sales,
            'sale_count': len(sales),
            'total_earned': total_earned
        }
        _card_sales_cache[cache_key] = (response, now + _CARD_SALES_TTL)
        return jsonify(response)

    except Exception as e:
        logger.error(f"❌ Error fetching card sale history: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/stream-history', methods=['GET'])
@learn_earn_token_required
def stream_history(current_user):
    """Return streaming payout history for current user."""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        result = supabase.table('learn_earn_streams')            .select('id, amount_gd, duration_seconds, flow_rate_wei, status, start_at, end_at, create_tx_hash, stop_tx_hash, created_at')            .eq('user_wallet', current_user.lower())            .order('created_at', desc=True)            .limit(50)            .execute()

        return jsonify({'success': True, 'streams': result.data or []}), 200
    except Exception as e:
        logger.error(f"❌ stream_history error: {e}")
        return jsonify({'success': False, 'error': 'Failed to fetch stream history'}), 500



@learn_earn_bp.route('/process-streams', methods=['POST'])
def process_streams():
    """Manually drive one stream worker cycle. Useful for ops / external cron.

    Delegates to ``streaming_service.process_streams_once`` so this endpoint
    and the in-process scheduler share the exact same atomic claim + on-chain
    settlement logic. Gated by ``LEARN_EARN_STREAM_WORKER_TOKEN`` for ops use;
    the in-process scheduler bypasses the token because it calls the function
    directly.
    """
    admin_token = os.getenv('LEARN_EARN_STREAM_WORKER_TOKEN', '')
    req_token = (request.headers.get('Authorization', '').replace('Bearer', '').strip())
    if not admin_token or req_token != admin_token:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    summary = streaming_service.process_streams_once()
    status_code = 200 if summary.get('success') else 503
    return jsonify(summary), status_code

@learn_earn_bp.route('/stats', methods=['GET'])
@learn_earn_token_required
def get_learn_earn_stats(current_user):
    """Get Learn & Earn system stats"""
    try:
        # Get Learn wallet balance
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            learn_balance = loop.run_until_complete(learn_blockchain_service.get_learn_wallet_balance())
            eligibility_info = loop.run_until_complete(quiz_manager.check_quiz_eligibility(current_user))
        finally:
            loop.close()

        # Get total questions available
        supabase = get_supabase_client()
        questions_result = supabase.table('quiz_questions').select('*').execute()
        total_questions = len(questions_result.data)

        # Smart contract integration will be added here
        contract_info = {'error': 'Contract integration disabled - using direct disbursement'}
        user_contract_stats = {}

        return jsonify({
            'success': True,
            'system_stats': {
                'learn_wallet_balance': learn_balance,
                'total_questions_available': total_questions,
                'questions_per_quiz': quiz_manager.questions_per_quiz,
                'reward_per_correct': quiz_manager.reward_per_correct,
                'max_reward_per_quiz': quiz_manager.questions_per_quiz * quiz_manager.reward_per_correct
            },
            'contract_info': contract_info,
            'user_contract_stats': user_contract_stats,
            'user_status': {
                'wallet_address': current_user,
                'can_take_quiz': eligibility_info.get('eligible', False),
                'ubi_verification': eligibility_info, # Consistent naming
                'eligible': eligibility_info.get('eligible', False)
            }
        }), 200

    except Exception as e:
        logger.error(f"❌ Error getting stats for {current_user}: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to get stats'
        }), 500

@learn_earn_bp.route('/contract-info', methods=['GET'])
def get_contract_info():
    """Get smart contract information"""
    try:
        # Smart contract integration will be added here
        contract_info = {
            'error': 'Contract integration disabled - using direct private key disbursement',
            'disbursement_method': 'direct_private_key'
        }

        return jsonify({
            'success': True,
            'contract_deployed': False,
            'contract_info': contract_info
        }), 200

    except Exception as e:
        logger.error(f"❌ Error getting contract info: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to get contract info'
        }), 500

@learn_earn_bp.route('/deposit-tokens', methods=['POST'])
@learn_earn_token_required
def deposit_tokens(current_user):
    """Deposit tokens to contract (admin only for now)"""
    try:
        # Smart contract integration will be added here
        return jsonify({
            'success': False,
            'error': 'Contract deposit not available - using direct private key disbursement',
            'disbursement_method': 'direct_private_key'
        }), 400

    except Exception as e:
        logger.error(f"❌ Error depositing tokens: {e}")
        return jsonify({
            'success': False,
            'error': 'Failed to deposit tokens'
        }), 500

@learn_earn_bp.route('/current-block', methods=['GET'])
def get_current_block():
    """Return the current Celo block number so the frontend knows where to start scanning from"""
    try:
        from web3 import Web3
        celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        w3 = Web3(Web3.HTTPProvider(celo_rpc_url, request_kwargs={'timeout': 15}))
        block = w3.eth.block_number
        return jsonify({'success': True, 'block': block})
    except Exception as e:
        logger.error(f"❌ Error getting current block: {e}")
        return jsonify({'success': False, 'block': 0}), 500


@learn_earn_bp.route('/collaboration/submissions', methods=['POST'])
def create_collaboration_submission():
    """Create a collaboration submission draft tied to the current wallet."""
    try:
        data = request.get_json(silent=True) or {}
        partner_name = (data.get('partner_name') or '').strip()
        if len(partner_name) < 2:
            return jsonify({'success': False, 'error': 'Partner name is required (minimum 2 characters).'}), 400

        wallet_address = _wallet_from_session_or_request(data)
        if not wallet_address or not wallet_address.startswith('0x') or len(wallet_address) != 42:
            return jsonify({'success': False, 'error': 'A valid wallet address is required.'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database unavailable'}), 500

        submission_id = uuid.uuid4().hex
        now_iso = datetime.utcnow().isoformat() + 'Z'
        row = {
            'id': submission_id,
            'wallet_address': _normalize_wallet(wallet_address),
            'partner_name': partner_name,
            'status': 'draft',
            'target_amount_gd': COLLABORATION_MIN_GD,
            'created_at': now_iso,
            'updated_at': now_iso
        }
        supabase.table('collaboration_submissions').insert(row).execute()
        return jsonify({'success': True, 'submission': row}), 201
    except Exception as e:
        logger.error(f"❌ Error creating collaboration submission: {e}")
        return jsonify({'success': False, 'error': 'Failed to create collaboration submission.'}), 500


@learn_earn_bp.route('/collaboration/submissions/<submission_id>', methods=['GET'])
def get_collaboration_submission(submission_id):
    """Get one collaboration submission with draft modules."""
    try:
        wallet = _normalize_wallet(session.get('wallet') or request.args.get('wallet_address', ''))
        if not wallet:
            return jsonify({'success': False, 'error': 'Wallet is required.'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database unavailable'}), 500

        sub_res = supabase.table('collaboration_submissions').select('*').eq('id', submission_id).limit(1).execute()
        if not sub_res.data:
            return jsonify({'success': False, 'error': 'Submission not found.'}), 404
        submission = sub_res.data[0]
        if _normalize_wallet(submission.get('wallet_address')) != wallet:
            return jsonify({'success': False, 'error': 'Unauthorized submission access.'}), 403

        mod_res = supabase.table('collaboration_modules')\
            .select('*')\
            .eq('submission_id', submission_id)\
            .eq('is_deleted', False)\
            .order('display_order', desc=False)\
            .execute()
        return jsonify({
            'success': True,
            'submission': submission,
            'modules': mod_res.data or []
        })
    except Exception as e:
        logger.error(f"❌ Error getting collaboration submission: {e}")
        return jsonify({'success': False, 'error': 'Failed to load collaboration submission.'}), 500


@learn_earn_bp.route('/collaboration/submissions/<submission_id>/modules', methods=['POST'])
def add_collaboration_module(submission_id):
    """Add a module draft for collaboration submission."""
    try:
        data = request.get_json(silent=True) or {}
        title = (data.get('title') or '').strip()
        url = (data.get('url') or '').strip()
        content = (data.get('content') or '').strip()
        display_order = int(data.get('display_order') or 1)
        if not title:
            return jsonify({'success': False, 'error': 'Title is required.'}), 400
        if not content and not url:
            return jsonify({'success': False, 'error': 'Provide content or URL.'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database unavailable'}), 500

        sub_res = supabase.table('collaboration_submissions').select('*').eq('id', submission_id).limit(1).execute()
        if not sub_res.data:
            return jsonify({'success': False, 'error': 'Submission not found.'}), 404
        submission = sub_res.data[0]
        if _normalize_wallet(submission.get('wallet_address')) != _normalize_wallet(session.get('wallet') or data.get('wallet_address')):
            return jsonify({'success': False, 'error': 'Unauthorized submission access.'}), 403

        reading_time = max(1, round(len((content or title).split()) / 200))
        now_iso = datetime.utcnow().isoformat() + 'Z'
        module_row = {
            'id': uuid.uuid4().hex,
            'submission_id': submission_id,
            'title': title,
            'url': url,
            'content': content,
            'reading_time_minutes': reading_time,
            'display_order': display_order,
            'is_active': True,
            'is_deleted': False,
            'created_at': now_iso,
            'updated_at': now_iso
        }
        supabase.table('collaboration_modules').insert(module_row).execute()
        return jsonify({'success': True, 'module': module_row}), 201
    except Exception as e:
        logger.error(f"❌ Error adding collaboration module: {e}")
        return jsonify({'success': False, 'error': 'Failed to add module.'}), 500


@learn_earn_bp.route('/collaboration/submissions/<submission_id>/modules/<module_id>', methods=['PUT'])
def update_collaboration_module(submission_id, module_id):
    """Update collaboration module draft fields only."""
    try:
        data = request.get_json(silent=True) or {}
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database unavailable'}), 500

        sub_res = supabase.table('collaboration_submissions').select('wallet_address,status').eq('id', submission_id).limit(1).execute()
        if not sub_res.data:
            return jsonify({'success': False, 'error': 'Submission not found.'}), 404
        submission = sub_res.data[0]
        if _normalize_wallet(submission.get('wallet_address')) != _normalize_wallet(session.get('wallet') or data.get('wallet_address')):
            return jsonify({'success': False, 'error': 'Unauthorized submission access.'}), 403
        if submission.get('status') in ('paid', 'published'):
            return jsonify({'success': False, 'error': 'Modules cannot be edited after payment.'}), 400

        update_data = {}
        for field in ('title', 'url', 'content', 'display_order', 'is_active'):
            if field in data:
                update_data[field] = data[field]
        if 'content' in update_data and isinstance(update_data['content'], str):
            update_data['reading_time_minutes'] = max(1, round(len(update_data['content'].split()) / 200))
        update_data['updated_at'] = datetime.utcnow().isoformat() + 'Z'

        result = supabase.table('collaboration_modules')\
            .update(update_data)\
            .eq('id', module_id)\
            .eq('submission_id', submission_id)\
            .execute()
        if not result.data:
            return jsonify({'success': False, 'error': 'Module not found.'}), 404
        return jsonify({'success': True, 'module': result.data[0]})
    except Exception as e:
        logger.error(f"❌ Error updating collaboration module: {e}")
        return jsonify({'success': False, 'error': 'Failed to update module.'}), 500


@learn_earn_bp.route('/collaboration/submissions/<submission_id>/modules/<module_id>', methods=['DELETE'])
def delete_collaboration_module(submission_id, module_id):
    """Soft-delete collaboration module draft; does not affect live admin modules."""
    try:
        data = request.get_json(silent=True) or {}
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database unavailable'}), 500
        sub_res = supabase.table('collaboration_submissions').select('wallet_address,status').eq('id', submission_id).limit(1).execute()
        if not sub_res.data:
            return jsonify({'success': False, 'error': 'Submission not found.'}), 404
        submission = sub_res.data[0]
        if _normalize_wallet(submission.get('wallet_address')) != _normalize_wallet(session.get('wallet') or data.get('wallet_address')):
            return jsonify({'success': False, 'error': 'Unauthorized submission access.'}), 403
        if submission.get('status') in ('paid', 'published'):
            return jsonify({'success': False, 'error': 'Modules cannot be deleted after payment.'}), 400

        result = supabase.table('collaboration_modules')\
            .update({'is_deleted': True, 'updated_at': datetime.utcnow().isoformat() + 'Z'})\
            .eq('id', module_id)\
            .eq('submission_id', submission_id)\
            .execute()
        if not result.data:
            return jsonify({'success': False, 'error': 'Module not found.'}), 404
        return jsonify({'success': True})
    except Exception as e:
        logger.error(f"❌ Error deleting collaboration module: {e}")
        return jsonify({'success': False, 'error': 'Failed to delete module.'}), 500


@learn_earn_bp.route('/collaboration/submissions/<submission_id>/begin-payment', methods=['POST'])
def begin_collaboration_payment(submission_id):
    """Move submission to awaiting_payment if it has at least one active module."""
    try:
        data = request.get_json(silent=True) or {}
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database unavailable'}), 500
        sub_res = supabase.table('collaboration_submissions').select('*').eq('id', submission_id).limit(1).execute()
        if not sub_res.data:
            return jsonify({'success': False, 'error': 'Submission not found.'}), 404
        submission = sub_res.data[0]
        if _normalize_wallet(submission.get('wallet_address')) != _normalize_wallet(session.get('wallet') or data.get('wallet_address')):
            return jsonify({'success': False, 'error': 'Unauthorized submission access.'}), 403

        mods = supabase.table('collaboration_modules')\
            .select('id')\
            .eq('submission_id', submission_id)\
            .eq('is_deleted', False)\
            .eq('is_active', True)\
            .execute()
        if not (mods.data and len(mods.data) > 0):
            return jsonify({'success': False, 'error': 'Add at least one module before payment.'}), 400

        upd = supabase.table('collaboration_submissions')\
            .update({'status': 'awaiting_payment', 'updated_at': datetime.utcnow().isoformat() + 'Z'})\
            .eq('id', submission_id)\
            .execute()
        payload = upd.data[0] if upd.data else submission
        return jsonify({
            'success': True,
            'submission': payload,
            'min_contribution': COLLABORATION_MIN_GD,
            'token': 'G$',
            'network': 'Celo Mainnet',
            'contract_address': _CONFIG_LEARN_EARN_ADDRESS
        })
    except Exception as e:
        logger.error(f"❌ Error beginning collaboration payment: {e}")
        return jsonify({'success': False, 'error': 'Failed to begin payment.'}), 500


@learn_earn_bp.route('/check-deposit', methods=['POST'])
def check_deposit():
    """Scan recent blocks for a G$ Transfer to the Learn & Earn contract from a given wallet.
    Returns the transaction data and generates a certificate if a qualifying deposit is found."""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'found': False, 'error': 'No data'}), 400

        wallet_address = data.get('wallet_address', '').strip()
        since_block = data.get('since_block', 0)
        sponsor_name = data.get('sponsor_name', '').strip()

        if not sponsor_name:
            return jsonify({'found': False, 'error': 'Sponsor name required'}), 400

        contract_address = _CONFIG_LEARN_EARN_ADDRESS
        gooddollar_address = _get_gooddollar_contract_address()
        celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')

        if not contract_address:
            return jsonify({'found': False, 'error': 'Contract address not configured'}), 500

        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(celo_rpc_url, request_kwargs={'timeout': 10}))
        gd_unit = 10 ** _get_gooddollar_decimals(w3, gooddollar_address)

        current_block = w3.eth.block_number
        blocks_per_2h = 1440  # Celo ~5s blocks, 2 hours lookback like proven P2P approach
        from_block = max(int(since_block), current_block - blocks_per_2h)

        transfer_topic = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
        erc20_checksum = Web3.to_checksum_address(gooddollar_address)
        contract_checksum = Web3.to_checksum_address(contract_address)

        def pad_addr(addr):
            return '0x' + addr[2:].lower().zfill(64)

        if not wallet_address or not wallet_address.startswith('0x') or len(wallet_address) != 42:
            return jsonify({'found': False, 'error': 'A valid Celo wallet address (0x...) is required to detect your deposit.'}), 400

        topics = [
            transfer_topic,
            pad_addr(Web3.to_checksum_address(wallet_address)),
            pad_addr(contract_checksum)
        ]

        try:
            logs = w3.eth.get_logs({
                'fromBlock': from_block,
                'toBlock': 'latest',
                'address': erc20_checksum,
                'topics': topics
            })
        except Exception as log_err:
            logger.warning(f"get_logs failed: {log_err}")
            return jsonify({'found': False}), 200

        if not logs:
            return jsonify({'found': False, 'scanned_to': current_block}), 200

        qualifying_log = None
        for log in reversed(logs):
            topics_list = log.get('topics', [])
            if len(topics_list) < 3:
                continue
            raw_amount = log.get('data', '0x0')
            if hasattr(raw_amount, 'hex'):
                raw_amount = raw_amount.hex()
            amount_wei = int(raw_amount, 16) if raw_amount and raw_amount != '0x' else 0
            amount_gd = amount_wei / gd_unit
            if amount_gd >= COLLABORATION_MIN_GD:
                qualifying_log = log
                break

        if not qualifying_log:
            return jsonify({'found': False, 'scanned_to': current_block}), 200

        tx_hash = qualifying_log['transactionHash'].hex() if hasattr(qualifying_log['transactionHash'], 'hex') else qualifying_log['transactionHash']
        if not tx_hash.startswith('0x'):
            tx_hash = '0x' + tx_hash

        raw_amount = qualifying_log.get('data', '0x0')
        if hasattr(raw_amount, 'hex'):
            raw_amount = raw_amount.hex()
        amount_wei = int(raw_amount, 16)
        verified_amount = amount_wei / gd_unit

        from_topic = qualifying_log['topics'][1].hex() if hasattr(qualifying_log['topics'][1], 'hex') else qualifying_log['topics'][1]
        sender_address = '0x' + from_topic[-40:]

        import uuid as _uuid
        cert_id = _uuid.uuid4().hex[:12]
        date_str = datetime.utcnow().strftime('%B %d, %Y')

        from .sponsor_certificate import generate_certificate
        cert_filename = generate_certificate(
            sponsor_name=sponsor_name,
            amount_gd=verified_amount,
            date_str=date_str,
            cert_id=cert_id,
            certificate_type='collaboration'
        )

        try:
            supabase = get_supabase_client()
            if supabase:
                masked_wallet = sender_address[:6] + '...' + sender_address[-4:] if len(sender_address) > 10 else sender_address
                supabase.table('sponsorship_log').insert({
                    'cert_id': cert_id,
                    'sponsor_name': sponsor_name,
                    'wallet_address': masked_wallet,
                    'tx_hash': tx_hash,
                    'amount_gd': verified_amount,
                    'date': date_str,
                    'cert_filename': cert_filename,
                    'created_at': datetime.utcnow().isoformat() + 'Z'
                }).execute()
        except Exception as db_err:
            logger.warning(f"Could not save sponsorship log: {db_err}")

        logger.info(f"✅ Auto-detected sponsorship: {sponsor_name} | {verified_amount:.2f} G$ | TX: {tx_hash}")

        return jsonify({
            'found': True,
            'tx_hash': tx_hash,
            'amount': verified_amount,
            'date': date_str,
            'cert_id': cert_id,
            'explorer_url': f'https://celoscan.io/tx/{tx_hash}',
            'download_url': f'/learn-earn/download-certificate/{cert_id}'
        })

    except Exception as e:
        logger.error(f"❌ Error in check-deposit: {e}")
        return jsonify({'found': False, 'error': 'Scan failed, retrying...'}), 200


@learn_earn_bp.route('/collaboration/submissions/<submission_id>/check-deposit', methods=['POST'])
def check_collaboration_deposit(submission_id):
    """Check collaboration payment and mark submission as paid once detected."""
    try:
        data = request.get_json(silent=True) or {}
        wallet_address = _wallet_from_session_or_request(data)
        partner_name = (data.get('partner_name') or '').strip()
        since_block = data.get('since_block', 0)
        submitted_tx_hash = (data.get('tx_hash') or '').strip()
        allow_test_amount = str(data.get('allow_test_amount', '')).strip().lower() in ('1', 'true', 'yes', 'on')
        expected_amount = data.get('expected_amount')

        try:
            expected_amount = float(expected_amount) if expected_amount is not None else None
        except (TypeError, ValueError):
            expected_amount = None

        if not partner_name:
            return jsonify({'success': False, 'error': 'Partner name required.'}), 400
        if not wallet_address or not wallet_address.startswith('0x') or len(wallet_address) != 42:
            return jsonify({'success': False, 'error': 'A valid wallet address is required.'}), 400

        is_admin_user = False
        try:
            from supabase_client import is_admin
            is_admin_user = bool(is_admin(wallet_address))
        except Exception as admin_err:
            logger.warning(f"⚠️ Could not resolve admin status for collaboration payment check: {admin_err}")

        if allow_test_amount and not is_admin_user:
            logger.warning(f"⚠️ Non-admin attempted test amount mode: {wallet_address[:8]}...")
            allow_test_amount = False

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database unavailable'}), 500
        sub_res = supabase.table('collaboration_submissions').select('*').eq('id', submission_id).limit(1).execute()
        if not sub_res.data:
            return jsonify({'success': False, 'error': 'Submission not found.'}), 404
        submission = sub_res.data[0]
        if _normalize_wallet(submission.get('wallet_address')) != _normalize_wallet(wallet_address):
            return jsonify({'success': False, 'error': 'Wallet does not match submission owner.'}), 403

        contract_address = _CONFIG_LEARN_EARN_ADDRESS
        gooddollar_address = _get_gooddollar_contract_address()
        celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(celo_rpc_url, request_kwargs={'timeout': 10}))
        gd_unit = 10 ** _get_gooddollar_decimals(w3, gooddollar_address)
        current_block = w3.eth.block_number
        from_block = max(int(since_block), current_block - 1440)

        transfer_topic = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
        erc20_checksum = Web3.to_checksum_address(gooddollar_address)
        contract_checksum = Web3.to_checksum_address(contract_address)
        topics = [
            transfer_topic,
            '0x' + Web3.to_checksum_address(wallet_address)[2:].lower().zfill(64),
            '0x' + contract_checksum[2:].lower().zfill(64)
        ]
        required_min = COLLABORATION_MIN_GD
        scan_min = required_min
        if allow_test_amount and expected_amount and expected_amount > 0:
            scan_min = expected_amount

        # Fast-path for wallet-submitted tx hash: verify receipt first so UI can show
        # "waiting for confirmation" instead of repeatedly reporting "no deposit yet".
        if submitted_tx_hash:
            try:
                receipt = w3.eth.get_transaction_receipt(submitted_tx_hash)
            except Exception:
                receipt = None

            if receipt is None:
                return jsonify({
                    'success': True,
                    'found': False,
                    'pending_confirmation': True,
                    'scanned_to': current_block,
                    'message': 'Transaction submitted and pending confirmation on Celo.'
                }), 200

            if receipt.status != 1:
                return jsonify({
                    'success': True,
                    'found': False,
                    'scanned_to': current_block,
                    'error': 'Submitted transaction failed on-chain. Please retry your payment.'
                }), 200

            transfer_topic_lower = transfer_topic.lower()
            normalized_wallet = Web3.to_checksum_address(wallet_address).lower()
            matched_token_transfer = False
            matched_sender = False
            matched_recipient = False
            max_amount_gd = 0.0
            matched_receipt_log = None
            for receipt_log in receipt.logs:
                log_address = receipt_log['address']
                log_address = log_address.lower() if isinstance(log_address, str) else log_address.hex().lower()
                if log_address != erc20_checksum.lower():
                    continue
                log_topics = receipt_log.get('topics', [])
                if len(log_topics) < 3:
                    continue
                first_topic = log_topics[0].hex() if hasattr(log_topics[0], 'hex') else log_topics[0]
                if not first_topic or first_topic.lower() != transfer_topic_lower:
                    continue

                matched_token_transfer = True

                from_topic = log_topics[1].hex() if hasattr(log_topics[1], 'hex') else log_topics[1]
                to_topic = log_topics[2].hex() if hasattr(log_topics[2], 'hex') else log_topics[2]
                from_addr = '0x' + from_topic[-40:]
                to_addr = '0x' + to_topic[-40:]
                if from_addr.lower() != normalized_wallet:
                    continue
                matched_sender = True
                if to_addr.lower() != contract_checksum.lower():
                    continue
                matched_recipient = True

                raw_amount = receipt_log.get('data', '0x0')
                if hasattr(raw_amount, 'hex'):
                    raw_amount = raw_amount.hex()
                amount_wei = int(raw_amount, 16) if raw_amount and raw_amount != '0x' else 0
                amount_gd = amount_wei / gd_unit
                max_amount_gd = max(max_amount_gd, amount_gd)
                if amount_gd >= scan_min:
                    matched_receipt_log = receipt_log
                    break

            if matched_receipt_log:
                logs = [matched_receipt_log]
            else:
                # Fallback (same behavior used by Reloadly flow):
                # even if the provided tx hash does not contain the final transfer,
                # continue scanning recent blocks for an actual G$ deposit event.
                try:
                    logs = w3.eth.get_logs({
                        'fromBlock': from_block,
                        'toBlock': 'latest',
                        'address': erc20_checksum,
                        'topics': topics
                    })
                except Exception:
                    logs = []

                if logs:
                    # Continue with normal qualifying-log flow below.
                    pass
                else:
                    if not matched_token_transfer:
                        root_error = 'Submitted transaction has no G$ Transfer event. Ensure you sent G$ token (not CELO/cUSD).'
                    elif not matched_sender:
                        root_error = 'Submitted transaction does not send G$ from the monitored wallet address.'
                    elif not matched_recipient:
                        root_error = 'Submitted transaction does not send G$ to the collaboration contract address.'
                    else:
                        root_error = (
                            f'Submitted transaction transfers G$, but amount is too low '
                            f'({max_amount_gd:,.2f} G$ < required {scan_min:,.2f} G$).'
                        )

                    return jsonify({
                        'success': True,
                        'found': False,
                        'scanned_to': current_block,
                        'error': root_error
                    }), 200
        else:
            logs = w3.eth.get_logs({
                'fromBlock': from_block,
                'toBlock': 'latest',
                'address': erc20_checksum,
                'topics': topics
            })
        if not logs:
            return jsonify({'success': True, 'found': False, 'scanned_to': current_block}), 200

        qualifying_log = None
        for log in reversed(logs):
            raw_amount = log.get('data', '0x0')
            if hasattr(raw_amount, 'hex'):
                raw_amount = raw_amount.hex()
            amount_wei = int(raw_amount, 16) if raw_amount and raw_amount != '0x' else 0
            amount_gd = amount_wei / gd_unit
            if amount_gd >= scan_min:
                qualifying_log = log
                break

        if not qualifying_log:
            return jsonify({
                'success': True,
                'found': False,
                'scanned_to': current_block,
                'error': f'No qualifying transfer found yet (minimum {scan_min:,.2f} G$).'
            }), 200

        tx_hash = qualifying_log['transactionHash'].hex() if hasattr(qualifying_log['transactionHash'], 'hex') else qualifying_log['transactionHash']
        if not tx_hash.startswith('0x'):
            tx_hash = '0x' + tx_hash
        raw_amount = qualifying_log.get('data', '0x0')
        if hasattr(raw_amount, 'hex'):
            raw_amount = raw_amount.hex()
        amount_wei = int(raw_amount, 16)
        verified_amount = amount_wei / gd_unit

        if verified_amount < required_min:
            # Persist the latest detected collaboration payment metadata even for
            # below-minimum "test" deposits so operators can still audit tx_hash
            # and amount in collaboration_submissions.
            supabase.table('collaboration_submissions')\
                .update({
                    'paid_amount_gd': verified_amount,
                    'tx_hash': tx_hash,
                    'updated_at': datetime.utcnow().isoformat() + 'Z'
                })\
                .eq('id', submission_id)\
                .execute()

            return jsonify({
                'success': True,
                'found': True,
                'test_only': True,
                'message': f'Test deposit detected ({verified_amount:,.2f} G$). Collaborator minimum remains {required_min:,.0f} G$.',
                'tx_hash': tx_hash,
                'amount': verified_amount,
                'date': datetime.utcnow().strftime('%B %d, %Y'),
                'explorer_url': f'https://celoscan.io/tx/{tx_hash}'
            }), 200

        cert_id = uuid.uuid4().hex[:12]
        date_str = datetime.utcnow().strftime('%B %d, %Y')
        from .sponsor_certificate import generate_certificate
        cert_filename = generate_certificate(
            sponsor_name=partner_name,
            amount_gd=verified_amount,
            date_str=date_str,
            cert_id=cert_id,
            certificate_type='collaboration'
        )

        supabase.table('collaboration_submissions')\
            .update({
                'status': 'paid',
                'paid_amount_gd': verified_amount,
                'tx_hash': tx_hash,
                'cert_id': cert_id,
                'cert_filename': cert_filename,
                'paid_at': datetime.utcnow().isoformat() + 'Z',
                'updated_at': datetime.utcnow().isoformat() + 'Z'
            })\
            .eq('id', submission_id)\
            .execute()

        automation_result = {
            'modules_total': 0,
            'modules_enriched': 0,
            'draft_questions_created': 0
        }
        try:
            from collaboration_automation import automate_collaboration_assets
            automation_result = automate_collaboration_assets(
                supabase=supabase,
                submission_id=submission_id,
                question_count=15
            )
        except Exception as automation_error:
            logger.warning(f"⚠️ Collaboration auto-automation after payment failed: {automation_error}")

        return jsonify({
            'success': True,
            'found': True,
            'tx_hash': tx_hash,
            'amount': verified_amount,
            'date': date_str,
            'cert_id': cert_id,
            'explorer_url': f'https://celoscan.io/tx/{tx_hash}',
            'download_url': f'/learn-earn/download-certificate/{cert_id}',
            'automation': automation_result
        }), 200
    except Exception as e:
        logger.error(f"❌ Error checking collaboration deposit: {e}")
        return jsonify({'success': False, 'error': 'Failed to check collaboration payment.'}), 500


@learn_earn_bp.route('/sponsor-contract-address', methods=['GET'])
def get_sponsor_contract_address():
    """Return the Learn & Earn contract address for public sponsorship display"""
    try:
        contract_address = _CONFIG_LEARN_EARN_ADDRESS
        return jsonify({
            'success': True,
            'contract_address': contract_address,
            'min_contribution': COLLABORATION_MIN_GD,
            'token': 'G$',
            'network': 'Celo Mainnet'
        })
    except Exception as e:
        logger.error(f"❌ Error fetching sponsor contract address: {e}")
        return jsonify({'success': False, 'error': 'Unable to fetch contract address'}), 500


@learn_earn_bp.route('/verify-sponsorship', methods=['POST'])
def verify_sponsorship():
    """Verify an on-chain sponsorship transaction and generate a certificate"""
    try:
        data = request.get_json()
        if not data:
            return jsonify({'success': False, 'error': 'No data provided'}), 400

        tx_hash = data.get('tx_hash', '').strip()
        sponsor_name = data.get('sponsor_name', '').strip()
        wallet_address = data.get('wallet_address', '').strip()

        if not tx_hash:
            return jsonify({'success': False, 'error': 'Transaction hash is required'}), 400
        if not sponsor_name:
            return jsonify({'success': False, 'error': 'Sponsor name is required'}), 400

        if not tx_hash.startswith('0x') or len(tx_hash) < 60:
            return jsonify({'success': False, 'error': 'Invalid transaction hash format'}), 400

        contract_address = _CONFIG_LEARN_EARN_ADDRESS
        gooddollar_address = _get_gooddollar_contract_address()
        celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')

        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(celo_rpc_url, request_kwargs={'timeout': 30}))
        gd_unit = 10 ** _get_gooddollar_decimals(w3, gooddollar_address)

        if not w3.is_connected():
            return jsonify({'success': False, 'error': 'Unable to connect to Celo network. Please try again.'}), 503

        try:
            receipt = w3.eth.get_transaction_receipt(tx_hash)
        except Exception:
            return jsonify({'success': False, 'error': 'Transaction not found. Please check the hash and try again.'}), 404

        if receipt is None:
            return jsonify({'success': False, 'error': 'Transaction not found on chain.'}), 404

        if receipt.status != 1:
            return jsonify({'success': False, 'error': 'Transaction failed on-chain. Only successful transactions qualify.'}), 400

        transfer_topic = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
        erc20_addr_checksum = Web3.to_checksum_address(gooddollar_address)
        contract_addr_checksum = Web3.to_checksum_address(contract_address) if contract_address else None

        verified_amount = 0.0
        found_transfer = False

        for log in receipt.logs:
            log_address = log['address'].lower() if isinstance(log['address'], str) else log['address'].hex().lower()
            if log_address != erc20_addr_checksum.lower():
                continue

            topics = log.get('topics', [])
            if not topics:
                continue
            first_topic = topics[0].hex() if hasattr(topics[0], 'hex') else topics[0]
            if first_topic.lower() != transfer_topic.lower():
                continue

            if len(topics) < 3:
                continue

            to_raw = topics[2].hex() if hasattr(topics[2], 'hex') else topics[2]
            to_addr = '0x' + to_raw[-40:]

            if contract_addr_checksum and to_addr.lower() != contract_addr_checksum.lower():
                continue

            amount_wei = int(log['data'].hex() if hasattr(log['data'], 'hex') else log['data'], 16)
            amount_gd = amount_wei / gd_unit

            if amount_gd < COLLABORATION_MIN_GD:
                return jsonify({
                    'success': False,
                    'error': f'Contribution amount ({amount_gd:.2f} G$) is below the minimum of {COLLABORATION_MIN_GD:,} G$.'
                }), 400

            verified_amount = amount_gd
            found_transfer = True
            break

        if not found_transfer:
            if not contract_addr_checksum:
                return jsonify({
                    'success': False,
                    'error': 'Contract address not configured. Please contact support.'
                }), 500
            return jsonify({
                'success': False,
                'error': 'No qualifying G$ transfer to the Learn & Earn contract was found in this transaction.'
            }), 400

        import uuid as _uuid
        cert_id = _uuid.uuid4().hex[:12]
        date_str = datetime.utcnow().strftime('%B %d, %Y')

        from .sponsor_certificate import generate_certificate
        cert_filename = generate_certificate(
            sponsor_name=sponsor_name,
            amount_gd=verified_amount,
            date_str=date_str,
            cert_id=cert_id,
            certificate_type='collaboration'
        )

        try:
            supabase = get_supabase_client()
            if supabase:
                supabase.table('sponsorship_log').insert({
                    'cert_id': cert_id,
                    'sponsor_name': sponsor_name,
                    'wallet_address': wallet_address[:6] + '...' + wallet_address[-4:] if len(wallet_address) > 10 else wallet_address,
                    'tx_hash': tx_hash,
                    'amount_gd': verified_amount,
                    'date': date_str,
                    'cert_filename': cert_filename,
                    'created_at': datetime.utcnow().isoformat() + 'Z'
                }).execute()
        except Exception as db_err:
            logger.warning(f"Could not save sponsorship log: {db_err}")

        explorer_url = f'https://celoscan.io/tx/{tx_hash}'
        logger.info(f"✅ Sponsorship verified: {sponsor_name} contributed {verified_amount:.2f} G$ TX: {tx_hash}")

        return jsonify({
            'success': True,
            'cert_id': cert_id,
            'cert_filename': cert_filename,
            'amount': verified_amount,
            'date': date_str,
            'explorer_url': explorer_url,
            'download_url': f'/learn-earn/download-certificate/{cert_id}'
        })

    except Exception as e:
        logger.error(f"❌ Error verifying sponsorship: {e}")
        return jsonify({'success': False, 'error': 'Verification failed. Please try again.'}), 500


@learn_earn_bp.route('/nft-balance', methods=['GET'])
@learn_earn_token_required
def get_nft_balance(current_user):
    """Get user's on-chain G$ balance for NFT marketplace"""
    try:
        now = time.time()
        cached = _nft_balance_cache.get(current_user)
        if cached and now < cached[1]:
            logger.info(f"📦 Using cached NFT balance for {current_user[:8]}...")
            return jsonify(cached[0])

        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(os.getenv('CELO_RPC_URL', 'https://forno.celo.org'), request_kwargs={'timeout': 10}))
        g_dollar_address = _GD_CONTRACT_ADDRESS
        erc20_abi = [{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}]
        token = w3.eth.contract(address=Web3.to_checksum_address(g_dollar_address), abi=erc20_abi)
        balance_wei = token.functions.balanceOf(Web3.to_checksum_address(current_user)).call()
        gd_unit = 10 ** _get_gooddollar_decimals(w3, g_dollar_address)
        balance = balance_wei / gd_unit
        result = {'success': True, 'balance': balance}
        _nft_balance_cache[current_user] = (result, now + _NFT_BALANCE_TTL)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting NFT balance: {e}")
        return jsonify({'success': True, 'balance': 0.0})


@learn_earn_bp.route('/mint-nft', methods=['POST'])
@learn_earn_token_required
def mint_achievement_nft(current_user):
    """Mint an Achievement NFT for a completed quiz"""
    try:
        from .nft_service import achievement_nft_service
        data = request.get_json()
        quiz_id = data.get('quiz_id', '')
        score = int(data.get('score', 0))
        total = int(data.get('total', 10))
        quiz_name = data.get('quiz_name', 'Learn & Earn Quiz')

        if not quiz_id:
            return jsonify({'success': False, 'error': 'Quiz ID is required'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        # Eligibility cutoff — only quizzes taken on or after Feb 11, 2026 can be minted
        MINT_ELIGIBLE_FROM = datetime(2026, 2, 11, 0, 0, 0)
        quiz_log_row = supabase.table('learnearn_log')\
            .select('timestamp')\
            .eq('quiz_id', quiz_id)\
            .eq('wallet_address', current_user)\
            .limit(1)\
            .execute()

        if quiz_log_row.data:
            raw_ts = quiz_log_row.data[0].get('timestamp', '')
            try:
                quiz_dt = datetime.fromisoformat(raw_ts.replace('Z', '+00:00')).replace(tzinfo=None)
                if quiz_dt < MINT_ELIGIBLE_FROM:
                    return jsonify({
                        'success': False,
                        'not_eligible': True,
                        'error': 'This quiz was taken before Feb 11, 2026 and is not eligible for NFT minting.'
                    }), 403
            except Exception:
                pass  # If we can't parse, allow through — backend already verified quiz_id ownership

        # Prevent duplicate mints per achievement card even if NFT ownership
        # has already been transferred to another wallet.
        existing = supabase.table('achievement_nft_mints')\
            .select('token_id, owner_wallet')\
            .eq('quiz_id', quiz_id)\
            .limit(1)\
            .execute()

        if existing.data and len(existing.data) > 0:
            return jsonify({
                'success': True,
                'already_minted': True,
                'token_id': existing.data[0]['token_id'],
                'current_owner': existing.data[0].get('owner_wallet')
            }), 200

        if not achievement_nft_service.is_configured:
            return jsonify({'success': False, 'not_deployed': True, 'error': 'NFT contract not deployed yet. Contact admin.'}), 503

        result = achievement_nft_service.mint_nft(current_user, quiz_id, score, total, quiz_name)
        if not result.get('success'):
            return jsonify({'success': False, 'error': result.get('error', 'Minting failed')}), 500

        token_id = result.get('token_id', 0)
        contract_address = _CONFIG_NFT_ADDRESS

        mint_data = {
            'token_id': token_id,
            'owner_wallet': current_user,
            'quiz_id': quiz_id,
            'quiz_name': quiz_name,
            'score': score,
            'total': total,
            'percentage': round((score / total) * 100) if total > 0 else 0,
            'tx_hash': result.get('tx_hash'),
            'contract_address': contract_address,
            'is_listed': False,
            'list_price': None,
            'minted_at': datetime.utcnow().isoformat() + 'Z'
        }
        supabase.table('achievement_nft_mints').insert(mint_data).execute()

        logger.info(f"✅ NFT #{token_id} minted for {current_user[:8]}... quiz={quiz_id}")
        return jsonify({
            'success': True,
            'token_id': token_id,
            'tx_hash': result.get('tx_hash'),
            'explorer_url': result.get('explorer_url')
        }), 200

    except Exception as e:
        logger.error(f"Error minting NFT: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/check-nft-minted', methods=['GET'])
@learn_earn_token_required
def check_nft_minted(current_user):
    """Check if a quiz has already been minted as an NFT, and whether it is eligible"""
    try:
        quiz_id = request.args.get('quiz_id', '')
        quiz_timestamp = request.args.get('quiz_timestamp', '')  # Optional: passed from frontend
        if not quiz_id:
            return jsonify({'success': False, 'already_minted': False, 'not_eligible': False}), 200

        MINT_ELIGIBLE_FROM = datetime(2026, 2, 11, 0, 0, 0)

        # Check eligibility via frontend-supplied timestamp (fast path)
        if quiz_timestamp:
            try:
                quiz_dt = datetime.fromisoformat(quiz_timestamp.replace('Z', '+00:00')).replace(tzinfo=None)
                if quiz_dt < MINT_ELIGIBLE_FROM:
                    return jsonify({
                        'success': True,
                        'already_minted': False,
                        'not_eligible': True,
                        'reason': 'Quiz taken before Feb 11, 2026'
                    }), 200
            except Exception:
                pass

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'already_minted': False, 'not_eligible': False}), 200

        # Also verify eligibility via DB timestamp if not supplied
        if not quiz_timestamp:
            quiz_log_row = supabase.table('learnearn_log')\
                .select('timestamp')\
                .eq('quiz_id', quiz_id)\
                .eq('wallet_address', current_user)\
                .limit(1)\
                .execute()
            if quiz_log_row.data:
                raw_ts = quiz_log_row.data[0].get('timestamp', '')
                try:
                    quiz_dt = datetime.fromisoformat(raw_ts.replace('Z', '+00:00')).replace(tzinfo=None)
                    if quiz_dt < MINT_ELIGIBLE_FROM:
                        return jsonify({
                            'success': True,
                            'already_minted': False,
                            'not_eligible': True,
                            'reason': 'Quiz taken before Feb 11, 2026'
                        }), 200
                except Exception:
                    pass

        # Achievement card can only be minted once globally per quiz attempt.
        # Query by quiz_id only so users who already minted then transferred/sold
        # still see this as already minted in UI.
        existing = supabase.table('achievement_nft_mints')\
            .select('token_id, minted_at, owner_wallet')\
            .eq('quiz_id', quiz_id)\
            .limit(1)\
            .execute()

        if existing.data and len(existing.data) > 0:
            return jsonify({
                'success': True,
                'already_minted': True,
                'not_eligible': False,
                'token_id': existing.data[0]['token_id'],
                'minted_at': existing.data[0].get('minted_at'),
                'current_owner': existing.data[0].get('owner_wallet')
            }), 200

        return jsonify({'success': True, 'already_minted': False, 'not_eligible': False}), 200

    except Exception as e:
        logger.error(f"Error checking NFT mint status: {e}")
        return jsonify({'success': False, 'already_minted': False, 'not_eligible': False}), 200


def _wallets_match(wallet_a, wallet_b):
    return (wallet_a or '').strip().lower() == (wallet_b or '').strip().lower()


def _sync_nft_owner_from_chain(supabase, nft_row, nft_service):
    if not nft_row or not nft_service.is_configured:
        return nft_row

    try:
        token_id = int(nft_row.get('token_id', 0))
        if token_id <= 0:
            return nft_row

        token_data = nft_service.get_token_data(token_id)
        chain_owner = (token_data.get('owner') or '').strip()
        if not chain_owner:
            return nft_row

        db_owner = (nft_row.get('owner_wallet') or '').strip()
        if _wallets_match(chain_owner, db_owner):
            return nft_row

        updates = {'owner_wallet': chain_owner, 'is_listed': False, 'list_price': None}
        supabase.table('achievement_nft_mints')\
            .update(updates)\
            .eq('token_id', token_id)\
            .execute()

        synced = dict(nft_row)
        synced.update(updates)
        logger.warning(f"Synced NFT #{token_id} owner from DB {db_owner[:8]}... to chain {chain_owner[:8]}...")
        return synced

    except Exception as sync_err:
        logger.warning(f"Could not sync NFT owner from chain: {sync_err}")
        return nft_row


def _release_nft_lock(supabase, token_id: int, lock_method: str):
    """
    Release the purchase lock on an NFT listing, restoring it to available state.
    Called whenever a purchase fails after the lock was acquired.
    """
    try:
        if lock_method == 'purchase_status':
            supabase.table('achievement_nft_mints')\
                .update({'purchase_status': None, 'purchase_locked_at': None})\
                .eq('token_id', token_id)\
                .execute()
        else:
            supabase.table('achievement_nft_mints')\
                .update({'is_listed': True})\
                .eq('token_id', token_id)\
                .execute()
        logger.info(f"🔓 NFT #{token_id} purchase lock released (method={lock_method})")
    except Exception as release_err:
        logger.error(f"❌ Failed to release purchase lock for NFT #{token_id}: {release_err}")


def _execute_nft_purchase_job(job_id: str, token_id: int, g_tx_hash: str,
                               buyer_wallet: str, seller_wallet: str,
                               list_price: float, nft: dict, lock_method: str):
    """
    Background thread: verifies G$ payment and transfers NFT.
    Updates nft_purchase_jobs table with status and result.
    """
    from .nft_service import achievement_nft_service
    from web3 import Web3

    supabase = get_supabase_client()
    if not supabase:
        logger.error(f"[job={job_id[:8]}] Cannot connect to database — lock will self-expire in 10 min via stale-lock cleanup")
        # Attempt a second fresh client in case of transient failure
        import time as _time
        _time.sleep(5)
        supabase = get_supabase_client()
    if not supabase:
        logger.error(f"[job={job_id[:8]}] Database unavailable after retry — aborting worker")
        return

    def _update_job(status, result=None, error=None):
        _set_memory_nft_job(job_id, buyer_wallet=buyer_wallet, status=status, result=result, error=error)
        try:
            update = {
                'status': status,
                'updated_at': datetime.now(timezone.utc).isoformat()
            }
            if result is not None:
                update['result'] = json.dumps(result)
            if error is not None:
                update['error_message'] = error
            if status in ('success', 'failed'):
                update['completed_at'] = datetime.now(timezone.utc).isoformat()
            supabase.table('nft_purchase_jobs').update(update).eq('job_id', job_id).execute()
        except Exception as upd_err:
            if _is_missing_nft_purchase_jobs_error(upd_err):
                logger.warning(f"[job={job_id[:8]}] nft_purchase_jobs table missing — using in-memory job status")
            else:
                logger.error(f"[job={job_id[:8]}] Failed to update job status: {upd_err}")

    try:
        _update_job('processing')

        swap_tx_hash = None
        g_tx_hash_final = g_tx_hash

        if achievement_nft_service.is_escrow_configured:
            # ── ESCROW PATH (atomic): single completeSwap() transaction ──────────
            # g_tx_hash is the buyer's approve(escrowAddr, price) tx hash.
            # We MUST wait for it to be mined before calling completeSwap(),
            # otherwise getAllowance() still returns the old (pre-approve) value.
            import time as _time
            from web3 import Web3 as _W3

            logger.info(
                f"[job={job_id[:8]}] ⏳ Waiting for buyer approve tx to be mined: "
                f"approve_tx={g_tx_hash[:18]}... buyer={buyer_wallet[:8]}..."
            )
            approve_mined = False
            _w3 = _W3(_W3.HTTPProvider(
                os.getenv('CELO_RPC_URL', 'https://forno.celo.org'),
                request_kwargs={'timeout': 10}
            ))
            # Poll for receipt: up to 36 attempts × 5 s = 180 s max wait
            for _attempt in range(36):
                try:
                    receipt = _w3.eth.get_transaction_receipt(g_tx_hash)
                    if receipt is not None:
                        if receipt.status != 1:
                            _release_nft_lock(supabase, token_id, lock_method)
                            _update_job('failed', error=f"Buyer approve tx reverted on-chain: {g_tx_hash[:18]}...")
                            return
                        # Safety check: verify the tx is an ERC-20 approve (0x095ea7b3),
                        # not a legacy direct transfer (0xa9059cbb). If buyer accidentally
                        # used an old cached page that sent G$ directly to the seller, we
                        # must fail fast here instead of silently losing G$ with no NFT.
                        try:
                            tx_data = _w3.eth.get_transaction(g_tx_hash)
                            tx_input = (tx_data.get('input') or tx_data.get('data') or b'')
                            if isinstance(tx_input, bytes):
                                tx_input_hex = tx_input.hex()
                            else:
                                tx_input_hex = str(tx_input).lower().replace('0x', '')
                            TRANSFER_SELECTOR = 'a9059cbb'
                            APPROVE_SELECTOR  = '095ea7b3'
                            if tx_input_hex.startswith(TRANSFER_SELECTOR):
                                _release_nft_lock(supabase, token_id, lock_method)
                                _update_job(
                                    'failed',
                                    error=(
                                        "Your wallet sent G$ directly to the seller instead of approving "
                                        "the escrow contract. This can happen with a stale/cached page. "
                                        "The seller has received your G$ — please contact support with "
                                        f"your G$ transaction hash ({g_tx_hash[:18]}...) to resolve this."
                                    )
                                )
                                logger.error(
                                    f"[job={job_id[:8]}] ❌ SAFETY: Buyer submitted a legacy TRANSFER tx "
                                    f"({g_tx_hash[:18]}...) in escrow mode — G$ sent to seller but NFT "
                                    f"NOT transferred. Manual intervention required."
                                )
                                return
                        except Exception as _tx_check_err:
                            logger.warning(f"[job={job_id[:8]}] Could not verify tx selector: {_tx_check_err}")
                        approve_mined = True
                        logger.info(f"[job={job_id[:8]}] ✅ Approve tx mined (attempt {_attempt+1}/36)")
                        break
                except Exception as _poll_err:
                    logger.warning(f"[job={job_id[:8]}] Receipt poll error (attempt {_attempt+1}/36): {_poll_err}")
                _time.sleep(5)

            if not approve_mined:
                logger.warning(f"[job={job_id[:8]}] Approve tx not found after 180 s — checking allowance directly")
                current_allowance = 0.0
                for _allowance_attempt in range(24):
                    current_allowance = achievement_nft_service.check_g_allowance(buyer_wallet)
                    if current_allowance >= list_price:
                        break
                    _time.sleep(5)
                if current_allowance < list_price:
                    _release_nft_lock(supabase, token_id, lock_method)
                    _update_job(
                        'failed',
                        error=(
                            f"Your G$ approval was not confirmed yet. Need {list_price:.4f} G$ approved, "
                            f"but only {current_allowance:.4f} G$ is visible on-chain. "
                            f"Please wait a moment, refresh the marketplace, and try again."
                        )
                    )
                    return
                logger.info(f"[job={job_id[:8]}] Allowance confirmed on-chain ({current_allowance:.4f} G$) — proceeding")

            logger.info(
                f"[job={job_id[:8]}] 🔄 Calling escrow completeSwap: "
                f"token=#{token_id} price={list_price} G$"
            )
            swap_result = achievement_nft_service.complete_swap(
                token_id, buyer_wallet, list_price
            )
            if not swap_result.get('success'):
                _release_nft_lock(supabase, token_id, lock_method)
                _update_job('failed', error=swap_result.get('error', 'Escrow atomic swap failed'))
                return
            swap_tx_hash = swap_result.get('tx_hash')
            g_tx_hash_final = g_tx_hash  # approve tx for audit trail

        else:
            # ── LEGACY PATH (two-step): verify G$ transfer, then transfer NFT ────
            # Active only when ESCROW_MARKETPLACE_ADDRESS env var is not set.
            # Once escrow contract is deployed and configured, this path is never reached.
            logger.warning(
                f"[job={job_id[:8]}] ⚠️  Using legacy two-step path (deploy escrow to enable atomic swaps). "
                f"tx={g_tx_hash[:18]}... buyer={buyer_wallet[:8]}... "
                f"seller={seller_wallet[:8]}... amount={list_price} G$"
            )
            logger.info(
                f"[job={job_id[:8]}] Verifying G$ payment: "
                f"tx={g_tx_hash[:18]}... buyer={buyer_wallet[:8]}... "
                f"seller={seller_wallet[:8]}... amount={list_price} G$"
            )
            g_verify = achievement_nft_service.verify_g_transfer(
                g_tx_hash, buyer_wallet, seller_wallet, list_price
            )
            if not g_verify.get('success'):
                _release_nft_lock(supabase, token_id, lock_method)
                _update_job('failed', error=g_verify.get('error', 'G$ payment verification failed'))
                return
            g_tx_hash_final = g_verify.get('tx_hash', g_tx_hash)

            nft_transfer = achievement_nft_service.transfer_nft(seller_wallet, buyer_wallet, token_id)
            if not nft_transfer.get('success'):
                logger.error(
                    f"[job={job_id[:8]}] NFT transfer failed after G$ was already sent! token={token_id}"
                )
                _release_nft_lock(supabase, token_id, lock_method)
                _update_job('failed', error=nft_transfer.get(
                    'error', f'NFT transfer failed — contact support with tx: {g_tx_hash[:18]}'
                ))
                return
            swap_tx_hash = nft_transfer.get('tx_hash')

        # Step 3: Update Supabase ownership — lock consumed
        ownership_update = {'owner_wallet': buyer_wallet, 'is_listed': False, 'list_price': None}
        if lock_method == 'purchase_status':
            ownership_update['purchase_status'] = 'sold'
            ownership_update['purchase_locked_at'] = None
        supabase.table('achievement_nft_mints').update(ownership_update).eq('token_id', token_id).execute()

        # Step 4: Record sale history
        try:
            supabase.table('nft_sale_history').insert({
                'token_id': token_id,
                'quiz_name': nft.get('quiz_name', ''),
                'seller_wallet': seller_wallet,
                'buyer_wallet': buyer_wallet,
                'price_g': list_price,
                'g_tx_hash': g_tx_hash_final,
                'nft_tx_hash': swap_tx_hash or ''
            }).execute()
        except Exception as history_err:
            logger.warning(f"[job={job_id[:8]}] Could not save sale history: {history_err}")

        # Step 5: Fetch updated buyer balance
        new_balance = None
        try:
            w3 = Web3(Web3.HTTPProvider(os.getenv('CELO_RPC_URL', 'https://forno.celo.org'), request_kwargs={'timeout': 10}))
            g_dollar_address = _GD_CONTRACT_ADDRESS
            erc20_abi = [{"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"}]
            token_contract = w3.eth.contract(address=Web3.to_checksum_address(g_dollar_address), abi=erc20_abi)
            new_balance = token_contract.functions.balanceOf(Web3.to_checksum_address(buyer_wallet)).call() / (10 ** 18)
        except Exception as bal_err:
            logger.warning(f"[job={job_id[:8]}] Could not fetch buyer balance: {bal_err}")

        result = {
            'token_id': token_id,
            'price': list_price,
            'new_balance': new_balance,
            'g_tx_hash': g_tx_hash_final,
            'nft_tx_hash': swap_tx_hash,
            'escrow_used': achievement_nft_service.is_escrow_configured
        }
        _update_job('success', result=result)
        flow = "escrow" if achievement_nft_service.is_escrow_configured else "legacy"
        logger.info(
            f"[job={job_id[:8]}] ✅ NFT #{token_id} sold ({flow}): "
            f"{seller_wallet[:8]}... -> {buyer_wallet[:8]}... for {list_price} G$"
        )

    except Exception as e:
        logger.error(f"[job={job_id[:8]}] Unexpected error in purchase job: {e}", exc_info=True)
        try:
            _release_nft_lock(supabase, token_id, lock_method)
        except Exception:
            pass
        _update_job('failed', error=str(e))


@learn_earn_bp.route('/nft-marketplace', methods=['GET'])
def get_nft_marketplace():
    """Get all NFTs listed for sale on the marketplace"""
    try:
        now = time.time()
        if _marketplace_cache["data"] is not None and now < _marketplace_cache["expires"]:
            logger.info("📦 Using cached NFT marketplace listings")
            return jsonify(_marketplace_cache["data"])

        from .nft_service import achievement_nft_service

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': True, 'listings': []})

        result = supabase.table('achievement_nft_mints')\
            .select('*')\
            .eq('is_listed', True)\
            .order('minted_at', desc=True)\
            .execute()

        listings = []
        contract_address = _CONFIG_NFT_ADDRESS
        for item in (result.data if result.data else []):
            item = _sync_nft_owner_from_chain(supabase, item, achievement_nft_service)
            if not item.get('is_listed'):
                continue
            if not item.get('contract_address'):
                item['contract_address'] = contract_address
            listings.append(item)

        response = {'success': True, 'listings': listings}
        _marketplace_cache["data"] = response
        _marketplace_cache["expires"] = now + _MARKETPLACE_TTL
        return jsonify(response)

    except Exception as e:
        logger.error(f"Error loading NFT marketplace: {e}")
        return jsonify({'success': True, 'listings': []})


@learn_earn_bp.route('/my-nfts', methods=['GET'])
@learn_earn_token_required
def get_my_nfts(current_user):
    """Get all NFTs owned by the current user"""
    try:
        now = time.time()
        cached = _my_nfts_cache.get(current_user)
        if cached and now < cached[1]:
            logger.info(f"📦 Using cached My NFTs for {current_user[:8]}...")
            return jsonify(cached[0])

        from .nft_service import achievement_nft_service

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': True, 'nfts': []})

        result = supabase.table('achievement_nft_mints')\
            .select('*')\
            .eq('owner_wallet', current_user)\
            .order('minted_at', desc=True)\
            .execute()

        nft_rows = {}
        for row in (result.data if result.data else []):
            nft_rows[int(row.get('token_id', 0))] = row

        if achievement_nft_service.is_configured:
            chain_token_ids = achievement_nft_service.get_owner_tokens(current_user)
            if chain_token_ids:
                try:
                    chain_rows = supabase.table('achievement_nft_mints')\
                        .select('*')\
                        .in_('token_id', chain_token_ids)\
                        .execute()
                    for row in (chain_rows.data if chain_rows.data else []):
                        token_id = int(row.get('token_id', 0))
                        if not _wallets_match(row.get('owner_wallet'), current_user):
                            supabase.table('achievement_nft_mints')\
                                .update({'owner_wallet': current_user, 'is_listed': False, 'list_price': None})\
                                .eq('token_id', token_id)\
                                .execute()
                            row['owner_wallet'] = current_user
                            row['is_listed'] = False
                            row['list_price'] = None
                        nft_rows[token_id] = row
                except Exception as chain_row_err:
                    logger.warning(f"Could not merge on-chain NFT ownership rows: {chain_row_err}")

        nfts = []
        contract_address = _CONFIG_NFT_ADDRESS
        for nft in nft_rows.values():
            nft = _sync_nft_owner_from_chain(supabase, nft, achievement_nft_service)
            if not _wallets_match(nft.get('owner_wallet'), current_user):
                continue
            if not nft.get('contract_address'):
                nft['contract_address'] = contract_address
            nfts.append(nft)

        response = {'success': True, 'nfts': nfts}
        _my_nfts_cache[current_user] = (response, time.time() + _MY_NFTS_TTL)
        return jsonify(response)

    except Exception as e:
        logger.error(f"Error getting user NFTs: {e}")
        return jsonify({'success': True, 'nfts': []})


@learn_earn_bp.route('/nft-list', methods=['POST'])
@learn_earn_token_required
def list_nft_for_sale(current_user):
    """List a user's NFT for sale on the marketplace"""
    try:
        from .nft_service import achievement_nft_service

        data = request.get_json(silent=True) or {}
        token_id = int(data.get('token_id', 0))
        price_g = float(data.get('price_g', 0))

        if token_id <= 0 or price_g <= 0:
            return jsonify({'success': False, 'error': 'Invalid token ID or price'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        nft = supabase.table('achievement_nft_mints')\
            .select('*')\
            .eq('token_id', token_id)\
            .execute()

        if not nft.data or len(nft.data) == 0:
            return jsonify({'success': False, 'error': 'NFT not found or not owned by you'}), 404

        nft_row = _sync_nft_owner_from_chain(supabase, nft.data[0], achievement_nft_service)
        if not _wallets_match(nft_row.get('owner_wallet'), current_user):
            return jsonify({'success': False, 'error': 'This NFT is currently owned by another wallet. Please refresh My NFTs.'}), 403

        supabase.table('achievement_nft_mints')\
            .update({'is_listed': True, 'list_price': price_g})\
            .eq('token_id', token_id)\
            .eq('owner_wallet', current_user)\
            .execute()

        logger.info(f"✅ NFT #{token_id} listed for {price_g} G$ by {current_user[:8]}...")

        # ── Register listing on-chain if escrow contract is deployed ─────────
        # CRITICAL: on-chain listing is REQUIRED for completeSwap() to work.
        # If it fails, rollback the DB listing so buyers cannot attempt a purchase
        # that will always fail (causing G$ to be lost with no NFT received).
        if achievement_nft_service.is_escrow_configured:
            escrow_result = achievement_nft_service.list_nft(token_id, current_user, price_g)
            if escrow_result.get('success'):
                logger.info(f"🔗 EscrowList on-chain confirmed: token=#{token_id} tx={escrow_result.get('tx_hash', '')[:18]}...")
            else:
                escrow_err = escrow_result.get('error', 'Unknown escrow error')
                logger.error(
                    f"❌ EscrowList on-chain FAILED for token #{token_id}: {escrow_err} "
                    f"— Rolling back DB listing to protect buyers."
                )
                # Rollback DB listing so buyers cannot trigger a doomed purchase
                supabase.table('achievement_nft_mints')\
                    .update({'is_listed': False, 'list_price': None})\
                    .eq('token_id', token_id)\
                    .eq('owner_wallet', current_user)\
                    .execute()
                return jsonify({
                    'success': False,
                    'error': (
                        f'Could not register your NFT listing on the blockchain: {escrow_err}. '
                        f'Please try listing again. If the problem continues, contact support.'
                    )
                }), 500

        # Invalidate marketplace and this user's my-nfts cache
        _marketplace_cache["data"] = None
        _marketplace_cache["expires"] = 0
        _my_nfts_cache.pop(current_user, None)

        return jsonify({
            'success': True,
            'message': f'NFT #{token_id} listed for {price_g} G$',
            'escrow_listed': achievement_nft_service.is_escrow_configured
        })

    except Exception as e:
        logger.error(f"Error listing NFT: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/nft-delist', methods=['POST'])
@learn_earn_token_required
def delist_nft(current_user):
    """Remove an NFT from the marketplace"""
    try:
        from .nft_service import achievement_nft_service

        data = request.get_json(silent=True) or {}
        token_id = int(data.get('token_id', 0))

        if token_id <= 0:
            return jsonify({'success': False, 'error': 'Invalid token ID'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        nft = supabase.table('achievement_nft_mints')\
            .select('*')\
            .eq('token_id', token_id)\
            .execute()

        if not nft.data or len(nft.data) == 0:
            return jsonify({'success': False, 'error': 'NFT not found'}), 404

        nft_row = _sync_nft_owner_from_chain(supabase, nft.data[0], achievement_nft_service)
        if not _wallets_match(nft_row.get('owner_wallet'), current_user):
            return jsonify({'success': False, 'error': 'This NFT is currently owned by another wallet. Please refresh My NFTs.'}), 403

        supabase.table('achievement_nft_mints')\
            .update({'is_listed': False, 'list_price': None})\
            .eq('token_id', token_id)\
            .eq('owner_wallet', current_user)\
            .execute()

        logger.info(f"✅ NFT #{token_id} delisted by {current_user[:8]}...")

        # ── Cancel on-chain listing if escrow contract is deployed ────────────
        if achievement_nft_service.is_escrow_configured:
            escrow_result = achievement_nft_service.cancel_listing(token_id)
            if escrow_result.get('success'):
                logger.info(f"🔗 EscrowCancel on-chain confirmed: token=#{token_id} tx={escrow_result.get('tx_hash', '')[:18]}...")
            else:
                logger.warning(
                    f"⚠️ EscrowCancel on-chain FAILED for token #{token_id}: {escrow_result.get('error')} "
                    f"— DB delisted but on-chain listing may still be active (harmless; swap requires DB check too)"
                )

        # Invalidate marketplace and this user's my-nfts cache
        _marketplace_cache["data"] = None
        _marketplace_cache["expires"] = 0
        _my_nfts_cache.pop(current_user, None)

        return jsonify({'success': True, 'message': f'NFT #{token_id} removed from marketplace'})

    except Exception as e:
        logger.error(f"Error delisting NFT: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/nft-burn', methods=['POST'])
@learn_earn_token_required
def burn_nft(current_user):
    """
    Burn an NFT and receive G$ back.
    Flow:
      1. Verify NFT ownership in Supabase
      2. Check LEARN_EARN contract has enough G$ balance
      3. Transfer NFT to dead address (burn) via transferByOperator
      4. Disburse G$ reward to user via LEARN_EARN contract
      5. Remove NFT record from Supabase
    Reward = (score / total) * 1000 G$
    """
    try:
        from .nft_service import achievement_nft_service

        data = request.get_json(silent=True) or {}
        token_id = int(data.get('token_id', 0))

        if token_id <= 0:
            return jsonify({'success': False, 'error': 'Invalid token ID'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        # 1. Verify ownership
        nft_res = supabase.table('achievement_nft_mints')\
            .select('token_id, quiz_id, quiz_name, score, total, is_listed, owner_wallet')\
            .eq('token_id', token_id)\
            .execute()

        if not nft_res.data or len(nft_res.data) == 0:
            return jsonify({'success': False, 'error': 'NFT not found or not owned by you'}), 404

        nft = _sync_nft_owner_from_chain(supabase, nft_res.data[0], achievement_nft_service)
        if not _wallets_match(nft.get('owner_wallet'), current_user):
            return jsonify({'success': False, 'error': 'This NFT is currently owned by another wallet. Please refresh My NFTs.'}), 403

        if nft.get('is_listed'):
            return jsonify({
                'success': False,
                'error': 'This NFT is listed on the marketplace. Please delist it first before burning.'
            }), 409

        score = int(nft.get('score', 0))
        total = int(nft.get('total', 1))
        quiz_name = nft.get('quiz_name', 'Achievement')
        quiz_id = nft.get('quiz_id', '')

        # 2. Calculate burn reward: (score/total) × 1000 G$
        burn_amount = round((score / total) * 1000, 2) if total > 0 else 0
        if burn_amount <= 0:
            return jsonify({'success': False, 'error': 'Cannot calculate burn reward'}), 400

        # 3. Check contract balance
        loop = asyncio.new_event_loop()
        contract_balance = loop.run_until_complete(learn_blockchain_service.get_contract_balance())
        loop.close()

        if contract_balance < burn_amount:
            logger.warning(f"Burn blocked — contract balance {contract_balance:.2f} G$ < needed {burn_amount:.2f} G$")
            return jsonify({
                'success': False,
                'error': f'Please try again once the contract has funds. (Balance: {contract_balance:.0f} G$, Needed: {burn_amount:.0f} G$)'
            }), 503

        # 4. Burn NFT on-chain (transfer to dead address)
        burn_result = achievement_nft_service.burn_nft(current_user, token_id)
        if not burn_result.get('success'):
            logger.error(f"NFT burn failed for #{token_id}: {burn_result.get('error')}")
            return jsonify({'success': False, 'error': burn_result.get('error', 'Failed to burn NFT')}), 500

        burn_tx_hash = burn_result.get('tx_hash', '')
        logger.info(f"🔥 NFT #{token_id} burned on-chain: {burn_tx_hash}")

        # 5. Disburse G$ to user
        import uuid
        burn_quiz_id = f"burn_{token_id}_{current_user[-8:].lower()}_{uuid.uuid4().hex[:8]}"
        loop2 = asyncio.new_event_loop()
        disburse_result = loop2.run_until_complete(
            learn_blockchain_service.disburse_quiz_reward(current_user, burn_amount, burn_quiz_id)
        )
        loop2.close()

        if not disburse_result.get('success'):
            logger.error(f"G$ disburse failed after burn for #{token_id}: {disburse_result.get('error')}")
            return jsonify({
                'success': False,
                'error': f"NFT burned but G$ transfer failed: {disburse_result.get('error')}. Contact support with burn TX: {burn_tx_hash}"
            }), 500

        reward_tx_hash = disburse_result.get('tx_hash', '')
        logger.info(f"💰 Burn reward {burn_amount} G$ → {current_user[:8]}... TX: {reward_tx_hash}")

        # 6. Save burn record to history table
        try:
            supabase.table('nft_burn_history').insert({
                'token_id': token_id,
                'quiz_name': quiz_name,
                'owner_wallet': current_user,
                'score': score,
                'total': total,
                'burn_amount_g': burn_amount,
                'burn_tx_hash': burn_tx_hash,
                'reward_tx_hash': reward_tx_hash,
            }).execute()
        except Exception as hist_err:
            logger.warning(f"Could not save burn history: {hist_err}")

        # 7. Remove NFT from Supabase
        supabase.table('achievement_nft_mints')\
            .delete()\
            .eq('token_id', token_id)\
            .execute()

        # Invalidate caches after successful burn
        _marketplace_cache["data"] = None
        _marketplace_cache["expires"] = 0
        _my_nfts_cache.pop(current_user, None)
        _nft_balance_cache.pop(current_user, None)

        logger.info(f"✅ Burn complete: NFT #{token_id} | {burn_amount} G$ → {current_user[:8]}...")
        return jsonify({
            'success': True,
            'message': f'NFT #{token_id} burned! {burn_amount:.0f} G$ sent to your wallet.',
            'burn_amount': burn_amount,
            'burn_tx_hash': burn_tx_hash,
            'reward_tx_hash': reward_tx_hash,
            'reward_explorer_url': disburse_result.get('explorer_url', '')
        })

    except Exception as e:
        logger.error(f"Error burning NFT: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/nft-operator-address', methods=['GET'])
def get_nft_operator_address():
    """Return the app wallet address so the frontend can build the approve() calldata"""
    try:
        from .nft_service import achievement_nft_service
        addr = achievement_nft_service.get_operator_address()
        if not addr:
            return jsonify({'success': False, 'error': 'Operator wallet not configured'}), 503
        return jsonify({'success': True, 'address': addr})
    except Exception as e:
        logger.error(f"Error getting operator address: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/nft-listing-status', methods=['GET'])
def get_nft_listing_status():
    try:
        from .nft_service import achievement_nft_service

        token_id = int(request.args.get('token_id', 0))
        if token_id <= 0:
            return jsonify({'success': False, 'error': 'Invalid token ID'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        nft_result = supabase.table('achievement_nft_mints')\
            .select('*')\
            .eq('token_id', token_id)\
            .execute()

        if not nft_result.data or len(nft_result.data) == 0:
            return jsonify({'success': False, 'error': 'NFT not found'}), 404

        nft = _sync_nft_owner_from_chain(supabase, nft_result.data[0], achievement_nft_service)
        if not nft.get('is_listed') or not nft.get('list_price'):
            return jsonify({'success': False, 'error': 'This listing is no longer available. Please refresh the marketplace.'}), 409

        return jsonify({
            'success': True,
            'token_id': token_id,
            'seller_wallet': nft.get('owner_wallet'),
            'list_price': nft.get('list_price'),
            'escrow_mode': achievement_nft_service.is_escrow_configured
        })

    except Exception as e:
        logger.error(f"Error checking NFT listing status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/nft-buy', methods=['POST'])
@learn_earn_token_required
def buy_nft(current_user):
    """
    Buy a listed NFT — non-blocking.
    Flow:
      1. Frontend sends {token_id, g_tx_hash} after signing the G$ transfer on-chain
      2. Backend validates, acquires atomic lock, creates a job, starts background thread
      3. Returns immediately with {success: True, job_id: ...}
      4. Client polls /nft-buy-status?job_id=... every 2 seconds for result
    """
    nft_locked = False
    lock_method = None
    token_id = 0
    supabase = None

    try:
        from .nft_service import achievement_nft_service
        data = request.get_json()
        token_id  = int(data.get('token_id', 0))
        g_tx_hash = (data.get('g_tx_hash') or '').strip()

        if token_id <= 0:
            return jsonify({'success': False, 'error': 'Invalid token ID'}), 400
        if not g_tx_hash or not g_tx_hash.startswith('0x'):
            return jsonify({'success': False, 'error': 'Page outdated — please hard-refresh the page (pull down to reload) and try again.'}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        # Invalidate marketplace cache immediately — NFT state is about to change
        _marketplace_cache["data"] = None
        _marketplace_cache["expires"] = 0
        _my_nfts_cache.pop(current_user, None)
        _nft_balance_cache.pop(current_user, None)

        nft_result = supabase.table('achievement_nft_mints')\
            .select('*')\
            .eq('token_id', token_id)\
            .eq('is_listed', True)\
            .execute()

        if not nft_result.data:
            return jsonify({'success': False, 'error': 'NFT not found or not listed for sale'}), 404

        nft = nft_result.data[0]
        nft = _sync_nft_owner_from_chain(supabase, nft, achievement_nft_service)
        if not nft.get('is_listed') or not nft.get('list_price'):
            return jsonify({'success': False, 'error': 'This listing is no longer available. Please refresh the marketplace.'}), 409

        seller_wallet = nft['owner_wallet']
        list_price = float(nft['list_price'])

        if seller_wallet.lower() == current_user.lower():
            return jsonify({'success': False, 'error': 'You cannot buy your own NFT'}), 400

        if not achievement_nft_service.is_configured:
            return jsonify({'success': False, 'error': 'NFT service not available'}), 503

        # --- SAFETY: Verify on-chain escrow listing is active before proceeding ---
        # This catches NFTs that are listed in the DB but whose on-chain escrow listing
        # failed silently (before the listing fix was deployed). Without this check,
        # the buyer's G$ approval would succeed but completeSwap would revert with
        # "No active on-chain listing", leaving the buyer without NFT.
        if achievement_nft_service.is_escrow_configured:
            try:
                on_chain_listing = achievement_nft_service.escrow_contract.functions.getListing(token_id).call()
                _, _, listing_active = on_chain_listing
                if not listing_active:
                    # Auto-rollback the DB listing so this broken state is cleaned up
                    try:
                        supabase.table('achievement_nft_mints')\
                            .update({'is_listed': False, 'list_price': None})\
                            .eq('token_id', token_id)\
                            .eq('is_listed', True)\
                            .execute()
                    except Exception:
                        pass
                    logger.error(
                        f"❌ BUY BLOCKED: NFT #{token_id} is listed in DB but has no active on-chain "
                        f"escrow listing — DB listing rolled back. Seller must re-list."
                    )
                    return jsonify({
                        'success': False,
                        'error': (
                            'This NFT listing is not fully registered on the blockchain yet. '
                            'The seller needs to re-list it. Please refresh the marketplace.'
                        )
                    }), 409
            except Exception as _listing_check_err:
                logger.warning(f"Could not verify on-chain listing for #{token_id}: {_listing_check_err} — proceeding")

        # --- ATOMIC LOCK: Prevent race conditions ---
        stale_lock_threshold = datetime.now(timezone.utc) - timedelta(minutes=10)

        try:
            try:
                supabase.table('achievement_nft_mints')\
                    .update({'purchase_status': None, 'purchase_locked_at': None})\
                    .eq('token_id', token_id)\
                    .eq('purchase_status', 'in_progress')\
                    .lt('purchase_locked_at', stale_lock_threshold.isoformat())\
                    .execute()
            except Exception:
                pass

            lock_result = supabase.table('achievement_nft_mints')\
                .update({'purchase_status': 'in_progress', 'purchase_locked_at': datetime.now(timezone.utc).isoformat()})\
                .eq('token_id', token_id)\
                .eq('is_listed', True)\
                .is_('purchase_status', 'null')\
                .eq('owner_wallet', seller_wallet)\
                .execute()
            nft_locked = bool(lock_result.data)
            lock_method = 'purchase_status'
        except Exception as lock_err:
            logger.warning(f"⚠️ purchase_status lock unavailable for NFT #{token_id}: {lock_err}. Falling back to is_listed lock.")
            lock_result = supabase.table('achievement_nft_mints')\
                .update({'is_listed': False})\
                .eq('token_id', token_id)\
                .eq('is_listed', True)\
                .eq('owner_wallet', seller_wallet)\
                .execute()
            nft_locked = bool(lock_result.data)
            lock_method = 'is_listed'

        if not nft_locked:
            logger.warning(f"⚠️ Race condition blocked: NFT #{token_id} already being purchased")
            return jsonify({'success': False, 'error': 'Hindi na available ang NFT — maaaring nabili na ito ng ibang buyer. I-refresh ang marketplace.'}), 409

        logger.info(f"🔒 NFT #{token_id} locked for purchase by {current_user[:8]}... (lock={lock_method})")

        # --- Create job record and start background thread ---
        job_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).isoformat()

        try:
            supabase.table('nft_purchase_jobs').insert({
                'job_id': job_id,
                'token_id': token_id,
                'buyer_wallet': current_user,
                'seller_wallet': seller_wallet,
                'price_g': list_price,
                'g_tx_hash': g_tx_hash,
                'status': 'pending',
                'purchase_locked_at': now_iso,
                'created_at': now_iso,
                'updated_at': now_iso
            }).execute()
        except Exception as job_insert_err:
            if _is_missing_nft_purchase_jobs_error(job_insert_err):
                logger.warning(f"⚠️ nft_purchase_jobs table missing — using in-memory status for job {job_id[:8]}")
                _set_memory_nft_job(job_id, buyer_wallet=current_user, status='pending')
            else:
                raise

        t = threading.Thread(
            target=_execute_nft_purchase_job,
            args=(job_id, token_id, g_tx_hash, current_user, seller_wallet, list_price, nft, lock_method),
            daemon=True
        )
        t.start()
        nft_locked = False  # Thread started — worker owns lock release from here

        logger.info(f"🚀 NFT purchase job {job_id[:8]} started for token #{token_id} by {current_user[:8]}")
        return jsonify({'success': True, 'job_id': job_id, 'status': 'pending'})

    except Exception as e:
        logger.error(f"Error creating NFT purchase job: {e}")
        if nft_locked and supabase and token_id > 0:
            _release_nft_lock(supabase, token_id, lock_method)
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/nft-buy-status', methods=['GET'])
@learn_earn_token_required
def get_nft_buy_status(current_user):
    """Check the status of a background NFT purchase job."""
    job_id = request.args.get('job_id', '').strip()
    if not job_id:
        return jsonify({'success': False, 'error': 'Missing job_id'}), 400

    supabase = get_supabase_client()
    if not supabase:
        memory_job = _get_memory_nft_job(job_id, current_user)
        if memory_job:
            return jsonify({
                'success': True,
                'job_id': job_id,
                'status': memory_job.get('status', 'pending'),
                'error_message': memory_job.get('error_message'),
                'result': memory_job.get('result')
            })
        return jsonify({'success': False, 'error': 'Database not available'}), 500

    try:
        result = supabase.table('nft_purchase_jobs')\
            .select('status, result, error_message')\
            .eq('job_id', job_id)\
            .eq('buyer_wallet', current_user)\
            .execute()

        if not result.data:
            return jsonify({'success': False, 'error': 'Job not found'}), 404

        job = result.data[0]
        response = {
            'success': True,
            'job_id': job_id,
            'status': job['status'],
            'error_message': job.get('error_message')
        }

        if job['status'] == 'success' and job.get('result'):
            try:
                response['result'] = job['result'] if isinstance(job['result'], dict) else json.loads(job['result'])
            except Exception:
                pass

        return jsonify(response)

    except Exception as e:
        if _is_missing_nft_purchase_jobs_error(e):
            memory_job = _get_memory_nft_job(job_id, current_user)
            if memory_job:
                return jsonify({
                    'success': True,
                    'job_id': job_id,
                    'status': memory_job.get('status', 'pending'),
                    'error_message': memory_job.get('error_message'),
                    'result': memory_job.get('result')
                })
            return jsonify({'success': False, 'error': 'Job not found'}), 404
        logger.error(f"Error checking purchase job status: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/nft-sale-history', methods=['GET'])
def get_nft_sale_history():
    """Get all NFT sale transaction history (public)"""
    try:
        limit = int(request.args.get('limit', 50))
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        result = supabase.table('nft_sale_history')\
            .select('*')\
            .order('sold_at', desc=True)\
            .limit(limit)\
            .execute()

        sales = result.data if result.data else []
        return jsonify({'success': True, 'sales': sales, 'count': len(sales)})

    except Exception as e:
        logger.error(f"❌ Error fetching NFT sale history: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/nft-burn-history', methods=['GET'])
@learn_earn_token_required
def get_nft_burn_history(current_user):
    """Get burn history for the logged-in user"""
    try:
        limit = int(request.args.get('limit', 50))
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({'success': False, 'error': 'Database not available'}), 500

        result = supabase.table('nft_burn_history')\
            .select('*')\
            .eq('owner_wallet', current_user)\
            .order('burned_at', desc=True)\
            .limit(limit)\
            .execute()

        burns = result.data if result.data else []
        return jsonify({'success': True, 'burns': burns, 'count': len(burns)})

    except Exception as e:
        logger.error(f"❌ Error fetching NFT burn history: {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


@learn_earn_bp.route('/download-certificate/<cert_id>', methods=['GET'])
def download_certificate(cert_id):
    """Download a generated sponsorship certificate PNG"""
    try:
        import re
        if not re.match(r'^[a-f0-9]{12}$', cert_id):
            return jsonify({'error': 'Invalid certificate ID'}), 400

        from flask import send_from_directory
        cert_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static', 'certificates')
        cert_filename = f'sponsorship_{cert_id}.png'
        cert_path = os.path.join(cert_dir, cert_filename)

        if not os.path.exists(cert_path):
            return jsonify({'error': 'Certificate not found'}), 404

        return send_from_directory(
            cert_dir,
            cert_filename,
            as_attachment=True,
            download_name=f'GoodDollar_Sponsorship_Certificate_{cert_id}.png'
        )
    except Exception as e:
        logger.error(f"❌ Error downloading certificate {cert_id}: {e}")
        return jsonify({'error': 'Download failed'}), 500


def init_learn_and_earn(app):
    """Initialize Learn & Earn module with Flask app"""
    try:
        logger.info("🎓 Initializing Learn & Earn module...")

        # Register Blueprint
        app.register_blueprint(learn_earn_bp)

        # Initialize sample questions synchronously like hour_bonus
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(quiz_manager.initialize_sample_questions())
            loop.close()
            logger.info("✅ Learn & Earn questions initialized")
        except Exception as init_error:
            logger.error(f"❌ Error initializing questions: {init_error}")

        logger.info("✅ Learn & Earn module initialized successfully")
        logger.info("📚 Available endpoints:")
        logger.info("   GET  /learn-earn/ - Dashboard")
        logger.info("   GET  /learn-earn/eligibility - Check user eligibility")
        logger.info("   POST /learn-earn/start-quiz - Start new quiz")
        logger.info("   POST /learn-earn/submit-quiz - Submit quiz answers")
        logger.info("   GET  /learn-earn/quiz-history - Get quiz history")
        logger.info("   GET  /learn-earn/stats - Get system stats")
        logger.info("   GET  /learn-earn/nft-balance - Get G$ balance for NFT marketplace")
        logger.info("   POST /learn-earn/mint-nft - Mint Achievement NFT")
        logger.info("   GET  /learn-earn/nft-marketplace - Browse listed NFTs")
        logger.info("   GET  /learn-earn/my-nfts - Get user's NFTs")
        logger.info("   POST /learn-earn/nft-list - List NFT for sale")
        logger.info("   POST /learn-earn/nft-delist - Remove NFT from sale")
        logger.info("   POST /learn-earn/nft-buy - Purchase a listed NFT")

        return True

    except Exception as e:
        logger.error(f"❌ Failed to initialize Learn & Earn module: {e}")
        return False

# Legacy functions for backward compatibility
def get_random_questions(count=10):
    """Legacy function for backward compatibility - now calls async method"""
    # This needs to run in an async context or manage its own loop.
    # For simplicity in a legacy context, we might assume it's called where an event loop is available.
    # If not, a new loop would need to be created and managed, which can be problematic.
    # A better approach would be to refactor calls to use the async version.
    # For now, let's assume a loop is available or create one if necessary.
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError: # No running loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

    try:
        return loop.run_until_complete(quiz_manager.get_random_questions(count))
    finally:
        # Avoid closing the loop if it was already running
        if loop.is_running():
            pass
        else:
            loop.close()

def calculate_score(user_answers):
    """Legacy function for backward compatibility - uses session data"""
    try:
        from flask import session
        questions = session.get('quiz_questions', [])
        if not questions:
            # If session data is missing, we cannot calculate score.
            # Consider logging an error or returning a specific error indicator.
            logger.error("Legacy calculate_score called without active quiz session.")
            return 0, 0

        correct_count = 0
        for i, user_answer in enumerate(user_answers):
            # Ensure we don't go out of bounds if user_answers is shorter/longer than expected
            if i < len(questions) and user_answer == questions[i].get('correct_answer'):
                correct_count += 1

        total_rewards = correct_count * quiz_manager.reward_per_correct
        return correct_count, total_rewards
    except RuntimeError:
        # Working outside of request context
        logger.error("Legacy calculate_score called outside Flask request context.")
        return 0, 0

def check_user_eligibility(wallet_address):
    """Legacy function for backward compatibility - now calls sync method"""
    try:
        # Use the sync method from quiz_manager instance
        return quiz_manager.check_user_eligibility(wallet_address)
    except Exception as e:
        logger.error(f"Error in legacy check_user_eligibility: {e}")
        return True # Default to True on error
