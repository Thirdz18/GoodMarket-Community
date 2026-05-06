import os
import logging
import time
from typing import Optional
from supabase import create_client, Client
from datetime import datetime
import json
from functools import wraps

# Configure logging — do NOT call basicConfig here; root logger level is set by main.py
logger = logging.getLogger(__name__)

# Supabase configuration
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_ANON_KEY")

# Initialize Supabase client with error handling
supabase: Client = None
supabase_enabled = False

def retry_on_connection_error(max_retries=3, delay=1):
    """Decorator to retry database operations on connection errors"""
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None

            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    error_msg = str(e).lower()

                    # Check if it's a connection-related error
                    if any(keyword in error_msg for keyword in ['server disconnected', 'connection', 'timeout', 'network']):
                        if attempt < max_retries - 1:
                            logger.warning(f"⚠️ Connection error on attempt {attempt + 1}/{max_retries}: {e}")
                            time.sleep(delay * (attempt + 1))  # Exponential backoff
                            continue
                        else:
                            logger.error(f"❌ All {max_retries} connection attempts failed: {e}")
                    else:
                        # Not a connection error, don't retry
                        raise e

            # If we get here, all retries failed
            raise last_exception
        return wrapper
    return decorator

def get_supabase_client():
    """Get Supabase client instance with retry logic for initialization"""
    global supabase, supabase_enabled

    if not SUPABASE_URL or not SUPABASE_KEY or SUPABASE_URL == "your-supabase-url":
        logger.warning("SUPABASE is not configured.")
        logger.warning(f"SUPABASE_URL exists: {bool(SUPABASE_URL)}")
        logger.warning(f"SUPABASE_KEY exists: {bool(SUPABASE_KEY)}")
        logger.warning("⚠️ Supabase not configured - using local analytics only")
        logger.info("💡 Set SUPABASE_URL and SUPABASE_ANON_KEY environment variables to enable Supabase logging")
        return None

    # Attempt to create client, with retries for initial connection
    for attempt in range(3): # Initial connection retries
        try:
            supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
            # Test connection by performing a simple query
            supabase.table("user_data").select("id").limit(1).execute()
            supabase_enabled = True
            logger.info("✅ Supabase client initialized successfully")
            return supabase
        except Exception as e:
            logger.error(f"❌ Supabase initialization failed on attempt {attempt + 1}: {e}")
            if attempt < 2:
                time.sleep(2) # Wait before retrying initialization
            else:
                logger.error("💡 Check your Supabase URL and API key in environment variables")
                supabase_enabled = False
                return None
    return None # Should not be reached if logic is sound

# Initialize the client
supabase = get_supabase_client()


# ─── Service-role client (server-side only) ─────────────────────────────────
#
# A separate client authenticated with SUPABASE_SERVICE_ROLE_KEY. This bypasses
# Row Level Security and is required for backend operations like uploading
# files to a private Storage bucket on behalf of a wallet user (who is *not*
# a Supabase Auth user).
#
# NEVER expose the service-role key to the frontend or commit it to git.

_supabase_admin: Optional[Client] = None
_supabase_admin_initialised = False


def get_supabase_admin_client():
    """Return a Supabase client authenticated with the service-role key.

    Returns ``None`` if ``SUPABASE_URL`` or ``SUPABASE_SERVICE_ROLE_KEY`` is
    not configured. Cached after the first successful creation.
    """
    global _supabase_admin, _supabase_admin_initialised

    if _supabase_admin_initialised:
        return _supabase_admin

    service_role_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not SUPABASE_URL or not service_role_key:
        logger.warning(
            "Supabase admin (service-role) client not configured. "
            "SUPABASE_URL exists=%s, SUPABASE_SERVICE_ROLE_KEY exists=%s",
            bool(SUPABASE_URL), bool(service_role_key),
        )
        _supabase_admin_initialised = True
        return None

    try:
        _supabase_admin = create_client(SUPABASE_URL, service_role_key)
        logger.info("✅ Supabase admin (service-role) client initialised")
    except Exception as exc:  # noqa: BLE001
        logger.error("❌ Supabase admin client init failed: %s", exc)
        _supabase_admin = None
    finally:
        _supabase_admin_initialised = True

    return _supabase_admin

# SQL COMMANDS TO RUN IN YOUR SUPABASE SQL EDITOR:
# Copy and run these commands one by one in your Supabase SQL Editor

"""
-- 0. IMPORTANT: Allow NULL transaction_hash for pending Telegram task submissions
-- Run this FIRST to fix the approval workflow:
ALTER TABLE telegram_task_log ALTER COLUMN transaction_hash DROP NOT NULL;

-- 0b. GoodMarket face-verification attribution columns
-- Run these to track users who verified AFTER first visiting GoodMarket as unverified:
ALTER TABLE user_data ADD COLUMN IF NOT EXISTS first_seen_unverified TIMESTAMP WITH TIME ZONE;
ALTER TABLE user_data ADD COLUMN IF NOT EXISTS verified_after_goodmarket BOOLEAN DEFAULT FALSE;
CREATE INDEX IF NOT EXISTS idx_user_data_verified_after_goodmarket ON user_data(verified_after_goodmarket);
CREATE INDEX IF NOT EXISTS idx_user_data_first_seen_unverified ON user_data(first_seen_unverified);

-- 0d. REFERRAL TRACKING COLUMNS IN user_data
-- Run these to link referral relationships directly in the user record:
-- referrer_wallet_address: the wallet that referred this user (set when a new user joins with a referral code)
-- referee_wallet_address:  this user's own wallet address as a referee (mirrors wallet_address for easy joins)
ALTER TABLE user_data ADD COLUMN IF NOT EXISTS referrer_wallet_address VARCHAR(42);
ALTER TABLE user_data ADD COLUMN IF NOT EXISTS referral_code_used VARCHAR(20);
ALTER TABLE user_data ADD COLUMN IF NOT EXISTS my_referral_code VARCHAR(20) UNIQUE;
ALTER TABLE user_data ADD COLUMN IF NOT EXISTS face_verified BOOLEAN DEFAULT FALSE;
ALTER TABLE user_data ADD COLUMN IF NOT EXISTS face_verified_at TIMESTAMP WITH TIME ZONE;
CREATE INDEX IF NOT EXISTS idx_user_data_referrer_wallet ON user_data(referrer_wallet_address);
CREATE INDEX IF NOT EXISTS idx_user_data_referral_code_used ON user_data(referral_code_used);
CREATE INDEX IF NOT EXISTS idx_user_data_my_referral_code ON user_data(my_referral_code);
CREATE INDEX IF NOT EXISTS idx_user_data_face_verified ON user_data(face_verified);

-- 0c. REFERRAL PROGRAM TABLES
-- Run these to enable the referral program:

CREATE TABLE IF NOT EXISTS referral_codes (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) UNIQUE NOT NULL,
    referral_code VARCHAR(20) UNIQUE NOT NULL,
    total_referrals INTEGER DEFAULT 0,
    total_earned NUMERIC DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_referral_codes_wallet ON referral_codes(wallet_address);
CREATE INDEX IF NOT EXISTS idx_referral_codes_code ON referral_codes(referral_code);

CREATE TABLE IF NOT EXISTS referrals (
    id SERIAL PRIMARY KEY,
    referral_code VARCHAR(20) NOT NULL,
    referrer_wallet VARCHAR(42) NOT NULL,
    referee_wallet VARCHAR(42) UNIQUE NOT NULL,
    status VARCHAR(30) DEFAULT 'pending_face_verification',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_referrals_code ON referrals(referral_code);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_wallet);
CREATE INDEX IF NOT EXISTS idx_referrals_referee ON referrals(referee_wallet);
CREATE INDEX IF NOT EXISTS idx_referrals_status ON referrals(status);

CREATE TABLE IF NOT EXISTS referral_rewards_log (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    reward_amount NUMERIC NOT NULL,
    reward_type VARCHAR(20) NOT NULL,
    referral_code VARCHAR(20) NOT NULL,
    tx_hash VARCHAR(66),
    status VARCHAR(20) DEFAULT 'pending',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_referral_rewards_wallet ON referral_rewards_log(wallet_address);
CREATE INDEX IF NOT EXISTS idx_referral_rewards_status ON referral_rewards_log(status);
CREATE INDEX IF NOT EXISTS idx_referral_rewards_code ON referral_rewards_log(referral_code);

-- 1. Create user_data table for user storage and counting
CREATE TABLE IF NOT EXISTS user_data (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) UNIQUE NOT NULL,
    first_login TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    last_login TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    total_logins INTEGER DEFAULT 1,
    total_sessions INTEGER DEFAULT 0,
    ubi_verified BOOLEAN DEFAULT FALSE,
    verification_timestamp TIMESTAMP WITH TIME ZONE,
    total_page_views INTEGER DEFAULT 0,
    user_agent TEXT,
    ip_address INET,
    username VARCHAR(50) UNIQUE,
    username_set_at TIMESTAMP WITH TIME ZONE,
    username_edited BOOLEAN DEFAULT FALSE,
    username_last_edited TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS email_wallet_links (
    id SERIAL PRIMARY KEY,
    email_hash VARCHAR(64) UNIQUE NOT NULL,
    wallet_address VARCHAR(42) NOT NULL,
    login_method VARCHAR(30) NOT NULL DEFAULT 'walletconnect',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_email_wallet_links_wallet ON email_wallet_links(wallet_address);

-- 1.1 Create news_articles table for news feed system
CREATE TABLE IF NOT EXISTS news_articles (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    content TEXT NOT NULL,
    category VARCHAR(50) DEFAULT 'announcement',
    priority VARCHAR(20) DEFAULT 'medium', -- 'low', 'medium', 'high'
    author VARCHAR(100) DEFAULT 'Admin',
    published BOOLEAN DEFAULT TRUE,
    featured BOOLEAN DEFAULT FALSE,
    image_url TEXT,
    url TEXT, -- External link URL (optional)
    view_count INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 1.2 Create Daily Check-in tables
CREATE TABLE IF NOT EXISTS daily_checkin_state (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) UNIQUE NOT NULL,
    current_streak INTEGER DEFAULT 0,
    last_checkin_date_utc DATE,
    total_daily_rewards NUMERIC DEFAULT 0,
    total_weekly_bonus_sent NUMERIC DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_daily_checkin_state_wallet ON daily_checkin_state(wallet_address);
CREATE INDEX IF NOT EXISTS idx_daily_checkin_state_last_checkin ON daily_checkin_state(last_checkin_date_utc);

CREATE TABLE IF NOT EXISTS daily_checkin_history (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    event_type VARCHAR(64) NOT NULL, -- daily_checkin | weekly_bonus_auto_sent | weekly_bonus_failed
    amount_celo NUMERIC NOT NULL DEFAULT 0,
    streak_before INTEGER DEFAULT 0,
    streak_after INTEGER DEFAULT 0,
    tx_hash VARCHAR(66),
    status VARCHAR(20) DEFAULT 'success',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_daily_checkin_history_wallet ON daily_checkin_history(wallet_address);
CREATE INDEX IF NOT EXISTS idx_daily_checkin_history_created_at ON daily_checkin_history(created_at DESC);

-- 2. Create user_sessions table for all activities and session tracking
CREATE TABLE IF NOT EXISTS user_sessions (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    activity_type VARCHAR(50) NOT NULL, -- 'login', 'logout', 'page_view', 'verification_attempt', 'ubi_activity'
    session_id VARCHAR(100),
    page VARCHAR(100),
    success BOOLEAN,
    details JSONB,
    ip_address INET,
    user_agent TEXT,
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Foreign key reference
    CONSTRAINT fk_user_sessions_wallet 
        FOREIGN KEY (wallet_address) 
        REFERENCES user_data(wallet_address) 
        ON DELETE CASCADE
);

-- 3. Create indexes for better performance
CREATE INDEX IF NOT EXISTS idx_user_data_wallet ON user_data(wallet_address);
CREATE INDEX IF NOT EXISTS idx_user_data_verified ON user_data(ubi_verified);
CREATE INDEX IF NOT EXISTS idx_user_sessions_wallet ON user_sessions(wallet_address);
CREATE INDEX IF NOT EXISTS idx_user_sessions_activity ON user_sessions(activity_type);
CREATE INDEX IF NOT EXISTS idx_user_sessions_timestamp ON user_sessions(timestamp);
CREATE INDEX IF NOT EXISTS idx_user_sessions_session_id ON user_sessions(session_id);

-- 3.1 Create indexes for news_articles table
CREATE INDEX IF NOT EXISTS idx_news_articles_published ON news_articles(published);
CREATE INDEX IF NOT EXISTS idx_news_articles_featured ON news_articles(featured);
CREATE INDEX IF NOT EXISTS idx_news_articles_category ON news_articles(category);
CREATE INDEX IF NOT EXISTS idx_news_articles_created_at ON news_articles(created_at);
CREATE INDEX IF NOT EXISTS idx_news_articles_priority ON news_articles(priority);

-- 3.2 Create reloadly_orders table for mobile top-up transactions
CREATE TABLE IF NOT EXISTS reloadly_orders (
    id SERIAL PRIMARY KEY,
    order_id VARCHAR(50) UNIQUE NOT NULL,
    wallet_address VARCHAR(42) NOT NULL,
    phone_number VARCHAR(20) NOT NULL,
    operator_id INTEGER NOT NULL,
    product_id VARCHAR(100),
    local_amount DECIMAL(10,2) NOT NULL,
    local_currency VARCHAR(10) NOT NULL,
    g_dollar_amount DECIMAL(18,8) NOT NULL,
    amount DECIMAL(18,8), -- backward compatibility
    status VARCHAR(50) DEFAULT 'pending_payment',
    merchant_address VARCHAR(42),
    payment_timeout TIMESTAMP WITH TIME ZONE,
    transaction_hash VARCHAR(66),
    reloadly_transaction_id VARCHAR(100),
    payment_confirmed_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- 3.3 Create indexes for reloadly_orders table
CREATE INDEX IF NOT EXISTS idx_reloadly_orders_wallet ON reloadly_orders(wallet_address);
CREATE INDEX IF NOT EXISTS idx_reloadly_orders_order_id ON reloadly_orders(order_id);
CREATE INDEX IF NOT EXISTS idx_reloadly_orders_status ON reloadly_orders(status);
CREATE INDEX IF NOT EXISTS idx_reloadly_orders_created_at ON reloadly_orders(created_at);

-- Referral tables removedH TIME ZONE DEFAULT NOW()
);

-- Referral indexes and policies removedds_log FOR ALL USING (true);

-- Create forum_rewards_log table for tracking post rewards
CREATE TABLE IF NOT EXISTS forum_rewards_log (
    id SERIAL PRIMARY KEY,
    transaction_hash VARCHAR(66) NOT NULL,
    post_id INTEGER NOT NULL,
    author_wallet VARCHAR(42) NOT NULL,
    reward_amount DECIMAL(18,8) NOT NULL,
    new_likes_rewarded INTEGER NOT NULL,
    rewarded_likes INTEGER NOT NULL,  -- Total likes rewarded so far
    reward_type VARCHAR(50) DEFAULT 'post_like',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create forum_like_rewards_log table for tracking like giver rewards
CREATE TABLE IF NOT EXISTS forum_like_rewards_log (
    id SERIAL PRIMARY KEY,
    transaction_hash VARCHAR(66) NOT NULL,
    post_id INTEGER NOT NULL,
    liker_wallet VARCHAR(42) NOT NULL,
    reward_amount DECIMAL(18,8) NOT NULL,
    reward_type VARCHAR(50) DEFAULT 'like_given',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for forum_rewards_log table
CREATE INDEX IF NOT EXISTS idx_forum_rewards_log_author ON forum_rewards_log(author_wallet);
CREATE INDEX IF NOT EXISTS idx_forum_rewards_log_post ON forum_rewards_log(post_id);
CREATE INDEX IF NOT EXISTS idx_forum_rewards_log_created_at ON forum_rewards_log(created_at);

-- Create indexes for forum_like_rewards_log table
CREATE INDEX IF NOT EXISTS idx_forum_like_rewards_log_liker ON forum_like_rewards_log(liker_wallet);
CREATE INDEX IF NOT EXISTS idx_forum_like_rewards_log_post ON forum_like_rewards_log(post_id);
CREATE INDEX IF NOT EXISTS idx_forum_like_rewards_log_created_at ON forum_like_rewards_log(created_at);

-- Enable RLS and create policies for forum_rewards_log
ALTER TABLE forum_rewards_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on forum_rewards_log" ON forum_rewards_log FOR ALL USING (true);

-- Enable RLS and create policies for forum_like_rewards_log
ALTER TABLE forum_like_rewards_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on forum_like_rewards_log" ON forum_like_rewards_log FOR ALL USING (true);

-- Create forum_comment_rewards_log table for tracking comment rewards
CREATE TABLE IF NOT EXISTS forum_comment_rewards_log (
    id SERIAL PRIMARY KEY,
    transaction_hash VARCHAR(66) NOT NULL,
    comment_id INTEGER NOT NULL,
    post_id INTEGER NOT NULL,
    commenter_wallet VARCHAR(42) NOT NULL,
    reward_amount DECIMAL(18,8) NOT NULL,
    reward_type VARCHAR(50) DEFAULT 'comment_made',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create forum_pending_rewards table for accumulating rewards before disbursement
CREATE TABLE IF NOT EXISTS forum_pending_rewards (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    pending_amount DECIMAL(18,8) DEFAULT 0,
    total_earned DECIMAL(18,8) DEFAULT 0,
    last_updated TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(wallet_address)
);

-- Create forum_reward_transactions table for tracking disbursements
CREATE TABLE IF NOT EXISTS forum_reward_transactions (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    transaction_hash VARCHAR(66) NOT NULL,
    amount_disbursed DECIMAL(18,8) NOT NULL,
    transaction_type VARCHAR(50) DEFAULT 'auto_disbursement', -- 'auto_disbursement', 'manual_withdrawal'
    status VARCHAR(50) DEFAULT 'completed',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for forum_comment_rewards_log table
CREATE INDEX IF NOT EXISTS idx_forum_comment_rewards_log_commenter ON forum_comment_rewards_log(commenter_wallet);
CREATE INDEX IF NOT EXISTS idx_forum_comment_rewards_log_comment ON forum_comment_rewards_log(comment_id);
CREATE INDEX IF NOT EXISTS idx_forum_comment_rewards_log_post ON forum_comment_rewards_log(post_id);
CREATE INDEX IF NOT EXISTS idx_forum_comment_rewards_log_created_at ON forum_comment_rewards_log(created_at);

-- Enable RLS and create policies for forum_comment_rewards_log
ALTER TABLE forum_comment_rewards_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on forum_comment_rewards_log" ON forum_comment_rewards_log FOR ALL USING (true);

-- Create indexes for forum_pending_rewards table
CREATE INDEX IF NOT EXISTS idx_forum_pending_rewards_wallet ON forum_pending_rewards(wallet_address);
CREATE INDEX IF NOT EXISTS idx_forum_pending_rewards_pending_amount ON forum_pending_rewards(pending_amount);
CREATE INDEX IF NOT EXISTS idx_forum_pending_rewards_last_updated ON forum_pending_rewards(last_updated);

-- Create indexes for forum_reward_transactions table
CREATE INDEX IF NOT EXISTS idx_forum_reward_transactions_wallet ON forum_reward_transactions(wallet_address);
CREATE INDEX IF NOT EXISTS idx_forum_reward_transactions_created_at ON forum_reward_transactions(created_at);
CREATE INDEX IF NOT EXISTS idx_forum_reward_transactions_status ON forum_reward_transactions(status);

-- Create community_screenshots table for community stories requirement examples
CREATE TABLE IF NOT EXISTS community_screenshots (
    id SERIAL PRIMARY KEY,
    screenshot_url TEXT NOT NULL,
    wallet_address VARCHAR(42) DEFAULT 'admin_requirement',
    title VARCHAR(200),
    image_type VARCHAR(50) DEFAULT 'requirement', -- 'requirement', 'user_submission'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for community_screenshots table
CREATE INDEX IF NOT EXISTS idx_community_screenshots_image_type ON community_screenshots(image_type);
CREATE INDEX IF NOT EXISTS idx_community_screenshots_wallet ON community_screenshots(wallet_address);
CREATE INDEX IF NOT EXISTS idx_community_screenshots_created_at ON community_screenshots(created_at);

-- Enable RLS and create policies for community_screenshots
ALTER TABLE community_screenshots ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on community_screenshots" ON community_screenshots FOR ALL USING (true);

-- Create forum_images table for storing uploaded images
CREATE TABLE IF NOT EXISTS forum_images (
    id SERIAL PRIMARY KEY,
    post_id INTEGER NOT NULL,
    image_url TEXT NOT NULL,
    uploaded_by VARCHAR(42) NOT NULL,
    upload_source VARCHAR(50) DEFAULT 'url', -- 'imgbb', 'url', 'other'
    image_size_bytes INTEGER,
    image_width INTEGER,
    image_height INTEGER,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),

    -- Foreign key reference
    CONSTRAINT fk_forum_images_post 
        FOREIGN KEY (post_id) 
        REFERENCES forum_posts(id) 
        ON DELETE CASCADE
);

-- Create indexes for forum_images table
CREATE INDEX IF NOT EXISTS idx_forum_images_post_id ON forum_images(post_id);
CREATE INDEX IF NOT EXISTS idx_forum_images_uploaded_by ON forum_images(uploaded_by);
CREATE INDEX IF NOT EXISTS idx_forum_images_created_at ON forum_images(created_at);

-- Enable RLS and create policies for forum_images
ALTER TABLE forum_images ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on forum_images" ON forum_images FOR ALL USING (true);

-- Create admin_broadcast_messages table for admin announcements
CREATE TABLE IF NOT EXISTS admin_broadcast_messages (
    id SERIAL PRIMARY KEY,
    title VARCHAR(200) NOT NULL,
    message TEXT NOT NULL,
    sender_wallet VARCHAR(42) NOT NULL,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- Create indexes for admin_broadcast_messages table
CREATE INDEX IF NOT EXISTS idx_admin_broadcast_messages_active ON admin_broadcast_messages(is_active);
CREATE INDEX IF NOT EXISTS idx_admin_broadcast_messages_created_at ON admin_broadcast_messages(created_at);
CREATE INDEX IF NOT EXISTS idx_admin_broadcast_messages_sender ON admin_broadcast_messages(sender_wallet);

-- Enable RLS and create policies for admin_broadcast_messages
ALTER TABLE admin_broadcast_messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on admin_broadcast_messages" ON admin_broadcast_messages FOR ALL USING (true);

-- Create trigger for auto-updating updated_at on admin_broadcast_messages
CREATE TRIGGER update_admin_broadcast_messages_updated_at 
    BEFORE UPDATE ON admin_broadcast_messages 
    FOR EACH ROW 
    EXECUTE FUNCTION update_updated_at_column();

-- Enable RLS and create policies for new forum tables
ALTER TABLE forum_pending_rewards ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on forum_pending_rewards" ON forum_pending_rewards FOR ALL USING (true);

ALTER TABLE forum_reward_transactions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on forum_reward_transactions" ON forum_reward_transactions FOR ALL USING (true);

-- Community Sponsorship Log Table
CREATE TABLE IF NOT EXISTS sponsorship_log (
    id SERIAL PRIMARY KEY,
    cert_id VARCHAR(20) UNIQUE NOT NULL,
    sponsor_name VARCHAR(200) NOT NULL,
    wallet_address VARCHAR(100),
    tx_hash VARCHAR(66) NOT NULL,
    amount_gd NUMERIC(18, 4) NOT NULL,
    date VARCHAR(50),
    cert_filename VARCHAR(200),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_sponsorship_log_cert_id ON sponsorship_log(cert_id);
CREATE INDEX IF NOT EXISTS idx_sponsorship_log_wallet ON sponsorship_log(wallet_address);
CREATE INDEX IF NOT EXISTS idx_sponsorship_log_tx_hash ON sponsorship_log(tx_hash);
CREATE INDEX IF NOT EXISTS idx_sponsorship_log_created_at ON sponsorship_log(created_at);
ALTER TABLE sponsorship_log ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on sponsorship_log" ON sponsorship_log FOR ALL USING (true);

-- Collaboration submissions (draft -> paid -> published flow)
CREATE TABLE IF NOT EXISTS collaboration_submissions (
    id VARCHAR(64) PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    partner_name VARCHAR(200) NOT NULL,
    status VARCHAR(30) DEFAULT 'draft',
    target_amount_gd NUMERIC(18, 4) DEFAULT 100000,
    paid_amount_gd NUMERIC(18, 4),
    tx_hash VARCHAR(66),
    cert_id VARCHAR(24),
    cert_filename VARCHAR(255),
    rejection_reason TEXT,
    paid_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_collab_sub_wallet ON collaboration_submissions(wallet_address);
CREATE INDEX IF NOT EXISTS idx_collab_sub_status ON collaboration_submissions(status);
CREATE INDEX IF NOT EXISTS idx_collab_sub_created_at ON collaboration_submissions(created_at);
ALTER TABLE collaboration_submissions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on collaboration_submissions" ON collaboration_submissions FOR ALL USING (true);
ALTER TABLE collaboration_submissions ADD COLUMN IF NOT EXISTS rejection_reason TEXT;

CREATE TABLE IF NOT EXISTS collaboration_modules (
    id VARCHAR(64) PRIMARY KEY,
    submission_id VARCHAR(64) NOT NULL,
    title VARCHAR(255) NOT NULL,
    url TEXT,
    content TEXT,
    reading_time_minutes INTEGER DEFAULT 1,
    display_order INTEGER DEFAULT 1,
    is_active BOOLEAN DEFAULT TRUE,
    is_deleted BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_collab_mod_submission_id ON collaboration_modules(submission_id);
CREATE INDEX IF NOT EXISTS idx_collab_mod_active ON collaboration_modules(is_active);
CREATE INDEX IF NOT EXISTS idx_collab_mod_deleted ON collaboration_modules(is_deleted);
ALTER TABLE collaboration_modules ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on collaboration_modules" ON collaboration_modules FOR ALL USING (true);

CREATE TABLE IF NOT EXISTS collaboration_quiz_questions_draft (
    id SERIAL PRIMARY KEY,
    submission_id VARCHAR(64) NOT NULL,
    question_id VARCHAR(100) NOT NULL,
    question TEXT NOT NULL,
    answer_a TEXT NOT NULL,
    answer_b TEXT NOT NULL,
    answer_c TEXT NOT NULL,
    answer_d TEXT NOT NULL,
    correct VARCHAR(1) NOT NULL,
    source_module_id VARCHAR(64),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_collab_q_submission_id ON collaboration_quiz_questions_draft(submission_id);
ALTER TABLE collaboration_quiz_questions_draft ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on collaboration_quiz_questions_draft" ON collaboration_quiz_questions_draft FOR ALL USING (true);

-- Achievement NFT Mints Table (for Learn & Earn NFT Marketplace)
CREATE TABLE IF NOT EXISTS achievement_nft_mints (
    id SERIAL PRIMARY KEY,
    token_id INTEGER NOT NULL,
    owner_wallet VARCHAR(42) NOT NULL,
    quiz_id VARCHAR(100) NOT NULL,
    quiz_name VARCHAR(200),
    score INTEGER DEFAULT 0,
    total INTEGER DEFAULT 10,
    percentage INTEGER DEFAULT 0,
    tx_hash VARCHAR(66),
    contract_address VARCHAR(42),
    is_listed BOOLEAN DEFAULT FALSE,
    list_price NUMERIC(18, 4),
    minted_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);
-- Non-blocking NFT purchase job tracking table
CREATE TABLE IF NOT EXISTS nft_purchase_jobs (
    id SERIAL PRIMARY KEY,
    job_id VARCHAR(36) UNIQUE NOT NULL,
    token_id INTEGER NOT NULL,
    buyer_wallet VARCHAR(42) NOT NULL,
    seller_wallet VARCHAR(42) NOT NULL,
    price_g NUMERIC(18, 4) NOT NULL,
    g_tx_hash VARCHAR(66) NOT NULL,
    status VARCHAR(20) DEFAULT 'pending',
    result JSONB,
    error_message TEXT,
    purchase_locked_at TIMESTAMP WITH TIME ZONE DEFAULT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    completed_at TIMESTAMP WITH TIME ZONE
);
CREATE INDEX IF NOT EXISTS idx_nft_purchase_jobs_job_id ON nft_purchase_jobs(job_id);
CREATE INDEX IF NOT EXISTS idx_nft_purchase_jobs_buyer ON nft_purchase_jobs(buyer_wallet);
CREATE INDEX IF NOT EXISTS idx_nft_purchase_jobs_status ON nft_purchase_jobs(status);
ALTER TABLE nft_purchase_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on nft_purchase_jobs" ON nft_purchase_jobs FOR ALL USING (true);
-- Add purchase_locked_at to existing nft_purchase_jobs tables (safe to re-run):
ALTER TABLE nft_purchase_jobs ADD COLUMN IF NOT EXISTS purchase_locked_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;

-- NFT Race Condition Fix: Add purchase_status and purchase_locked_at columns for atomic locking
-- Run these AFTER the CREATE TABLE above (safe to run even if table already exists):
ALTER TABLE achievement_nft_mints ADD COLUMN IF NOT EXISTS purchase_status VARCHAR(20) DEFAULT NULL;
ALTER TABLE achievement_nft_mints ADD COLUMN IF NOT EXISTS purchase_locked_at TIMESTAMP WITH TIME ZONE DEFAULT NULL;
CREATE INDEX IF NOT EXISTS idx_achievement_nft_mints_purchase_status ON achievement_nft_mints(purchase_status);
CREATE INDEX IF NOT EXISTS idx_achievement_nft_mints_owner ON achievement_nft_mints(owner_wallet);
CREATE INDEX IF NOT EXISTS idx_achievement_nft_mints_token_id ON achievement_nft_mints(token_id);
CREATE INDEX IF NOT EXISTS idx_achievement_nft_mints_quiz_id ON achievement_nft_mints(quiz_id);
CREATE INDEX IF NOT EXISTS idx_achievement_nft_mints_is_listed ON achievement_nft_mints(is_listed);
CREATE INDEX IF NOT EXISTS idx_achievement_nft_mints_minted_at ON achievement_nft_mints(minted_at);
ALTER TABLE achievement_nft_mints ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on achievement_nft_mints" ON achievement_nft_mints FOR ALL USING (true);

-- Task Completion System Tables
CREATE TABLE IF NOT EXISTS task_completion_log (
    id SERIAL PRIMARY KEY,
    transaction_hash VARCHAR(66) NOT NULL,
    wallet_address VARCHAR(42) NOT NULL,
    task_id VARCHAR(50) NOT NULL,
    task_type VARCHAR(50) NOT NULL,
    reward_amount DECIMAL(18,8) NOT NULL,
    status VARCHAR(20) DEFAULT 'completed',
    verification_method VARCHAR(50),
    timestamp TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

-- User task progress table
CREATE TABLE IF NOT EXISTS user_task_progress (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    task_id VARCHAR(50) NOT NULL,
    progress JSONB DEFAULT '{}',
    completed_at TIMESTAMP WITH TIME ZONE,
    last_attempt TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    streak_count INTEGER DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    UNIQUE(wallet_address, task_id)
);

-- Create indexes for task completion system
CREATE INDEX IF NOT EXISTS idx_task_completion_log_wallet ON task_completion_log(wallet_address);
CREATE INDEX IF NOT EXISTS idx_task_completion_log_task_id ON task_completion_log(task_id);
CREATE INDEX IF NOT EXISTS idx_task_completion_log_timestamp ON task_completion_log(timestamp);
CREATE INDEX IF NOT EXISTS idx_user_task_progress_wallet ON user_task_progress(wallet_address);
CREATE INDEX IF NOT EXISTS idx_user_task_progress_task_id ON user_task_progress(task_id);

-- Enable RLS for task completion system
ALTER TABLE task_completion_log ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_task_progress ENABLE ROW LEVEL SECURITY;

-- Create policies for task completion system
CREATE POLICY "Allow all operations on task_completion_log" ON task_completion_log FOR ALL USING (true);
CREATE POLICY "Allow all operations on user_task_progress" ON user_task_progress FOR ALL USING (true);

-- 4. Enable Row Level Security (optional but recommended)
ALTER TABLE user_data ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_sessions ENABLE ROW LEVEL SECURITY;
ALTER TABLE news_articles ENABLE ROW LEVEL SECURITY;
ALTER TABLE reloadly_orders ENABLE ROW LEVEL SECURITY;

-- 5. Create policies to allow read/write access (adjust as needed)
CREATE POLICY "Allow all operations on user_data" ON user_data FOR ALL USING (true);
CREATE POLICY "Allow all operations on user_sessions" ON user_sessions FOR ALL USING (true);
CREATE POLICY "Allow all operations on news_articles" ON news_articles FOR ALL USING (true);
CREATE POLICY "Allow all operations on reloadly_orders" ON reloadly_orders FOR ALL USING (true);

-- 6. Create function to auto-update updated_at timestamp
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ language 'plpgsql';

-- 7. Create trigger for auto-updating updated_at
CREATE TRIGGER update_user_data_updated_at 
    BEFORE UPDATE ON user_data 
    FOR EACH ROW 
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_news_articles_updated_at 
    BEFORE UPDATE ON news_articles 
    FOR EACH ROW 
    EXECUTE FUNCTION update_updated_at_column();

CREATE TRIGGER update_reloadly_orders_updated_at 
    BEFORE UPDATE ON reloadly_orders 
    FOR EACH ROW 
    EXECUTE FUNCTION update_updated_at_column();

-- Payment links table for Send via Link feature
CREATE TABLE IF NOT EXISTS payment_links (
    id SERIAL PRIMARY KEY,
    wallet_address VARCHAR(42) NOT NULL,
    payment_id VARCHAR(42) NOT NULL UNIQUE,
    private_key_enc TEXT NOT NULL,
    amount VARCHAR(64) NOT NULL,
    tx_hash VARCHAR(66),
    status VARCHAR(20) DEFAULT 'active',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_payment_links_wallet ON payment_links(wallet_address);
CREATE INDEX IF NOT EXISTS idx_payment_links_payment_id ON payment_links(payment_id);
CREATE INDEX IF NOT EXISTS idx_payment_links_status ON payment_links(status);

ALTER TABLE payment_links ENABLE ROW LEVEL SECURITY;
CREATE POLICY "Allow all operations on payment_links" ON payment_links FOR ALL USING (true);

CREATE TRIGGER update_payment_links_updated_at
    BEFORE UPDATE ON payment_links
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();
"""

class SupabaseLogger:
    def __init__(self):
        self.client = supabase
        self.enabled = supabase_enabled

    def mask_wallet_address(self, wallet_address: str) -> str:
        """Mask wallet address for logging"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    def create_or_update_user(self, wallet_address: str, session_data: dict = None):
        """Create or update user in user_data table"""
        if not self.enabled:
            logger.warning("⚠️ Supabase not enabled - skipping user logging")
            return None

        try:
            # Normalize address to checksum format before any DB operation
            try:
                from web3 import Web3
                if Web3.is_address(wallet_address):
                    wallet_address = Web3.to_checksum_address(wallet_address)
            except Exception:
                pass

            # Case-insensitive lookup so 0xAbCd... and 0xabcd... find the same record
            existing_user = self.client.table("user_data")\
                .select("*")\
                .ilike("wallet_address", wallet_address)\
                .execute()

            if existing_user.data:
                # Update existing user
                user_id = existing_user.data[0]["id"]
                current_logins = existing_user.data[0].get("total_logins", 0)
                current_sessions = existing_user.data[0].get("total_sessions", 0)

                result = self.client.table("user_data")\
                    .update({
                        "last_login": datetime.now().isoformat(),
                        "total_logins": current_logins + 1,
                        "total_sessions": current_sessions + 1,
                        "user_agent": session_data.get("user_agent") if session_data else None,
                        "ip_address": session_data.get("ip_address") if session_data else None
                    })\
                    .eq("id", user_id)\
                    .execute()

                logger.info(f"✅ Updated user login #{current_logins + 1} for wallet: {wallet_address}")
            else:
                # Generate this user's own referral code deterministically
                own_referral_code = None
                try:
                    import hashlib, string as _string
                    seed = f"goodmarket-referral-{wallet_address.lower()}"
                    digest = hashlib.sha256(seed.encode()).hexdigest()
                    chars = _string.ascii_uppercase + _string.digits
                    own_referral_code = ''.join(
                        chars[int(digest[i:i+2], 16) % len(chars)] for i in range(0, 16, 2)
                    )[:8]
                except Exception:
                    own_referral_code = None

                # Create new user
                user_record = {
                    "wallet_address": wallet_address,
                    "first_login": datetime.now().isoformat(),
                    "last_login": datetime.now().isoformat(),
                    "total_logins": 1,
                    "total_sessions": 1,
                    "ubi_verified": False,
                    "total_page_views": 0,
                    "user_agent": session_data.get("user_agent") if session_data else None,
                    "ip_address": session_data.get("ip_address") if session_data else None
                }
                if own_referral_code:
                    user_record["my_referral_code"] = own_referral_code

                result = self.client.table("user_data").insert(user_record).execute()
                logger.info(f"✅ Created new user for wallet: {wallet_address}"
                            + (f" | referral code: {own_referral_code}" if own_referral_code else ""))

            return result

        except Exception as e:
            logger.error(f"❌ Error creating/updating user: {e}")
            return None

    def log_activity(self, wallet_address: str, activity_type: str, session_id: str = None, 
                    page: str = None, success: bool = None, details: dict = None, 
                    session_data: dict = None):
        """Log any activity to user_sessions table"""
        if not self.enabled:
            return None

        try:
            # Normalize address to checksum format
            try:
                from web3 import Web3
                if Web3.is_address(wallet_address):
                    wallet_address = Web3.to_checksum_address(wallet_address)
            except Exception:
                pass

            # Ensure user exists in user_data before logging activity (case-insensitive)
            existing_user = self.client.table("user_data")\
                .select("wallet_address")\
                .ilike("wallet_address", wallet_address)\
                .execute()

            if not existing_user.data:
                logger.warning(f"⚠️ User {wallet_address} not found in user_data, creating...")
                self.create_or_update_user(wallet_address, session_data)

            activity_record = {
                "wallet_address": wallet_address,
                "activity_type": activity_type,
                "session_id": session_id,
                "page": page,
                "success": success,
                "details": details or {},
                "ip_address": session_data.get("ip_address") if session_data else None,
                "user_agent": session_data.get("user_agent") if session_data else None,
                "timestamp": datetime.now().isoformat()
            }

            result = self.client.table("user_sessions").insert(activity_record).execute()
            logger.info(f"✅ Logged {activity_type} for wallet: {wallet_address}")
            return result

        except Exception as e:
            logger.error(f"❌ Error logging activity: {e}")
            return None

    def log_login(self, wallet_address: str, session_data: dict = None):
        """Log user login - combines user_data update and session logging"""
        session_id = f"session_{wallet_address}_{int(datetime.now().timestamp())}"

        # Update user data
        self.create_or_update_user(wallet_address, session_data)

        # Log login activity
        return self.log_activity(
            wallet_address=wallet_address,
            activity_type="login",
            session_id=session_id,
            success=True,
            details={"action": "user_login"},
            session_data=session_data
        )

    def save_referrer_wallet(self, referee_wallet: str, referrer_wallet: str,
                             referral_code: str = None) -> None:
        """
        Store the referrer's wallet address and the referral code on the
        referee's user_data record. Called when a new user successfully
        connects with a valid referral code.
        Only writes if the columns are not already set (prevents overwriting).
        """
        if not self.enabled:
            return
        try:
            from web3 import Web3
            if Web3.is_address(referee_wallet):
                referee_wallet = Web3.to_checksum_address(referee_wallet)
            if Web3.is_address(referrer_wallet):
                referrer_wallet = Web3.to_checksum_address(referrer_wallet)
        except Exception:
            pass

        try:
            existing = self.client.table("user_data")\
                .select("referrer_wallet_address, referral_code_used")\
                .ilike("wallet_address", referee_wallet)\
                .execute()

            if existing.data:
                row = existing.data[0]
                update_payload = {}
                if row.get("referrer_wallet_address") is None:
                    update_payload["referrer_wallet_address"] = referrer_wallet
                if row.get("referral_code_used") is None and referral_code:
                    update_payload["referral_code_used"] = referral_code.upper()

                if update_payload:
                    self.client.table("user_data")\
                        .update(update_payload)\
                        .ilike("wallet_address", referee_wallet)\
                        .execute()
                    logger.info(f"✅ Saved referral data for {referee_wallet[:10]}...: "
                                f"referrer={referrer_wallet[:10]}... code={referral_code}")
        except Exception as e:
            logger.error(f"❌ Error saving referrer_wallet_address: {e}")

    def log_verification_attempt(self, wallet_address: str, success: bool, details: dict = None, face_verified: bool = False):
        """Log UBI verification attempt.

        face_verified=True means the user ACTUALLY completed GoodDollar face verification
        (confirmed via the on-chain identity contract).
        success=True without face_verified means the user was granted GoodMarket access only.
        Only face_verified=True updates ubi_verified, face_verified, and verified_after_goodmarket.
        """
        # Normalize address
        try:
            from web3 import Web3
            if Web3.is_address(wallet_address):
                wallet_address = Web3.to_checksum_address(wallet_address)
        except Exception:
            pass

        # Ensure user exists before logging activity
        self.create_or_update_user(wallet_address)

        result = self.log_activity(
            wallet_address=wallet_address,
            activity_type="verification_attempt",
            success=success,
            details=details or {}
        )

        if not self.enabled:
            return result

        if success and face_verified:
            # User ACTUALLY completed GoodDollar face verification
            try:
                user_row = self.client.table("user_data")\
                    .select("first_seen_unverified, verified_after_goodmarket, "
                            "first_login, created_at, face_verified_at")\
                    .ilike("wallet_address", wallet_address)\
                    .execute()

                update_payload = {
                    "ubi_verified": True,
                    "verification_timestamp": datetime.now().isoformat(),
                    "face_verified": True,
                    "face_verified_at": datetime.now().isoformat()
                }

                if user_row.data:
                    row = user_row.data[0]
                    # Strict attribution: only credit GoodMarket when the on-chain
                    # ``lastAuthenticated`` actually falls within this GoodMarket
                    # session (within STRICT_ATTRIBUTION_WINDOW_SECONDS of "now")
                    # AND the user came to GoodMarket BEFORE verifying. Without
                    # this guard, /fv-callback flips the flag for users who
                    # verified months ago elsewhere and just round-tripped
                    # through GoodMarket's FV button — see the audit summary in
                    # GOODMARKET_ATTRIBUTION_AUDIT.md (only 2/123 of the rows
                    # under the old code actually verified through GoodMarket).
                    if not row.get("verified_after_goodmarket"):
                        try:
                            from goodmarket_attribution_backfill import (
                                is_attributable_to_goodmarket,
                            )
                            decision = is_attributable_to_goodmarket(wallet_address, row)
                        except Exception as attr_err:  # noqa: BLE001
                            logger.warning(
                                f"⚠️ Attribution check failed for {wallet_address}: {attr_err}"
                            )
                            decision = {"attributable": False, "reason": "helper_error"}

                        if decision.get("attributable"):
                            update_payload["verified_after_goodmarket"] = True
                            logger.info(
                                f"🏆 GoodMarket-attributed face verification for wallet: "
                                f"{wallet_address} (delta={decision.get('delta_seconds')}s)"
                            )
                        else:
                            logger.info(
                                f"ℹ️ /fv-callback fired for {wallet_address} but "
                                f"attribution not credited "
                                f"(reason={decision.get('reason')})"
                            )
                    # Also backfill first_seen_unverified if it was never recorded
                    if not row.get("first_seen_unverified"):
                        update_payload["first_seen_unverified"] = datetime.now().isoformat()
                        logger.info(f"📝 Backfilled first_seen_unverified on FV callback for wallet: {wallet_address}")

                self.client.table("user_data")\
                    .update(update_payload)\
                    .ilike("wallet_address", wallet_address)\
                    .execute()

                logger.info(f"✅ Updated face verification status for wallet: {wallet_address}")
            except Exception as e:
                logger.error(f"❌ Error updating verification status: {e}")
        elif success and not face_verified:
            # GoodMarket login access only — do NOT touch ubi_verified or verified_after_goodmarket
            logger.info(f"ℹ️ GoodMarket access granted (no face verification check) for: {wallet_address}")
        else:
            # Verification failed — record first time this user was seen as unverified
            try:
                user_row = self.client.table("user_data")\
                    .select("first_seen_unverified")\
                    .ilike("wallet_address", wallet_address)\
                    .execute()

                if user_row.data and user_row.data[0].get("first_seen_unverified") is None:
                    self.client.table("user_data")\
                        .update({"first_seen_unverified": datetime.now().isoformat()})\
                        .ilike("wallet_address", wallet_address)\
                        .execute()
                    logger.info(f"📝 Recorded first_seen_unverified for wallet: {wallet_address}")
            except Exception as e:
                logger.error(f"❌ Error recording first_seen_unverified: {e}")

        return result

    def record_unverified_visit(self, wallet_address: str):
        """Record that a user visited GoodMarket while NOT yet face-verified on GoodDollar.
        Only sets first_seen_unverified if it hasn't been set yet.
        Does NOT count as a verification attempt — purely a tracking record."""
        if not self.enabled:
            return

        try:
            from web3 import Web3
            if Web3.is_address(wallet_address):
                wallet_address = Web3.to_checksum_address(wallet_address)
        except Exception:
            pass

        try:
            # Ensure user exists first
            self.create_or_update_user(wallet_address)

            user_row = self.client.table("user_data")\
                .select("first_seen_unverified, ubi_verified")\
                .ilike("wallet_address", wallet_address)\
                .execute()

            if user_row.data:
                row = user_row.data[0]
                # Only set if not already recorded and user is not already marked verified
                if row.get("first_seen_unverified") is None and not row.get("ubi_verified"):
                    self.client.table("user_data")\
                        .update({"first_seen_unverified": datetime.now().isoformat()})\
                        .ilike("wallet_address", wallet_address)\
                        .execute()
                    logger.info(f"📝 Recorded unverified visit for wallet: {wallet_address}")
        except Exception as e:
            logger.error(f"❌ Error recording unverified visit: {e}")

    def log_page_view(self, wallet_address: str, page: str, session_data: dict = None):
        """Log page view and update user_data page view count"""
        result = self.log_activity(
            wallet_address=wallet_address,
            activity_type="page_view",
            page=page,
            details={"page_accessed": page},
            session_data=session_data
        )

        # Update user_data page view count
        if self.enabled:
            try:
                user_data = self.client.table("user_data")\
                    .select("total_page_views")\
                    .eq("wallet_address", wallet_address)\
                    .execute()

                if user_data.data:
                    current_views = user_data.data[0].get("total_page_views", 0)
                    self.client.table("user_data")\
                        .update({"total_page_views": current_views + 1})\
                        .eq("wallet_address", wallet_address)\
                        .execute()
            except Exception as e:
                logger.error(f"❌ Error updating page view count: {e}")

        return result

    def log_logout(self, wallet_address: str, session_data: dict = None):
        """Log user logout"""
        return self.log_activity(
            wallet_address=wallet_address,
            activity_type="logout",
            details={"action": "user_logout"},
            session_data=session_data
        )

    def log_ubi_activity(self, wallet_address: str, ubi_details: dict = None):
        """Log UBI-related activity"""
        return self.log_activity(
            wallet_address=wallet_address,
            activity_type="ubi_activity",
            success=True,
            details=ubi_details or {}
        )

    def get_user_stats(self, wallet_address: str):
        """Get comprehensive user statistics"""
        if not self.enabled:
            return {}

        try:
            # Get user data
            user_result = self.client.table("user_data")\
                .select("*")\
                .eq("wallet_address", wallet_address)\
                .execute()

            # Get recent sessions
            sessions_result = self.client.table("user_sessions")\
                .select("*")\
                .eq("wallet_address", wallet_address)\
                .order("timestamp", desc=True)\
                .limit(20)\
                .execute()

            user_data = user_result.data[0] if user_result.data else {}
            sessions_data = sessions_result.data

            return {
                "user_info": user_data,
                "recent_activities": sessions_data,
                "activity_counts": self._count_activities(sessions_data)
            }

        except Exception as e:
            logger.error(f"❌ Error getting user stats: {e}")
            return {}

    def get_analytics_summary(self):
        """Get comprehensive analytics summary from Supabase data"""
        try:
            # Get total count of users using count query (more efficient)
            count_response = self.client.table("user_data").select("*", count="exact").execute()
            total_users = count_response.count if hasattr(count_response, 'count') else 0

            # Get verified users count (ubi_verified = legacy flag, face_verified = accurate newer flag)
            verified_response = self.client.table("user_data").select("*", count="exact").eq("ubi_verified", True).execute()
            verified_users = verified_response.count if hasattr(verified_response, 'count') else 0

            # Total face-verified users (the most accurate count of GoodDollar face-verified users)
            try:
                fv_response = self.client.table("user_data").select("*", count="exact").eq("face_verified", True).execute()
                face_verified_total = fv_response.count if hasattr(fv_response, 'count') else 0
            except Exception:
                face_verified_total = verified_users  # fallback to ubi_verified count

            # Get users who verified AFTER first visiting GoodMarket as unverified
            # These are users GoodMarket can claim credit for motivating to verify
            try:
                goodmarket_verified_response = self.client.table("user_data")\
                    .select("*", count="exact")\
                    .eq("verified_after_goodmarket", True)\
                    .execute()
                goodmarket_verified_users = goodmarket_verified_response.count if hasattr(goodmarket_verified_response, 'count') else 0
            except Exception:
                goodmarket_verified_users = 0

            # Get users who visited GoodMarket but are still unverified (potential conversions)
            try:
                pending_verification_response = self.client.table("user_data")\
                    .select("*", count="exact")\
                    .not_.is_("first_seen_unverified", "null")\
                    .eq("ubi_verified", False)\
                    .execute()
                pending_verification_users = pending_verification_response.count if hasattr(pending_verification_response, 'count') else 0
            except Exception:
                pending_verification_users = 0

            # Ensure GoodMarket-attributed buckets are reflected in overall totals,
            # even when legacy flags are delayed/incomplete for some records.
            total_users_adjusted = max(
                total_users,
                face_verified_total + pending_verification_users,
                goodmarket_verified_users + pending_verification_users
            )

            # Calculate verification rate
            verification_rate = (
                f"{(face_verified_total / total_users_adjusted * 100):.1f}%"
                if total_users_adjusted > 0 else "0%"
            )

            # Calculate GoodMarket attribution rate (of unverified users who eventually verified)
            unverified_first_seen = goodmarket_verified_users + pending_verification_users
            goodmarket_conversion_rate = f"{(goodmarket_verified_users / unverified_first_seen * 100):.1f}%" if unverified_first_seen > 0 else "0%"

            # Get total page views by summing from all users
            try:
                all_users_response = self.client.table("user_data").select("total_page_views").execute()
                total_page_views = sum(user.get("total_page_views", 0) for user in all_users_response.data) if all_users_response.data else 0
            except Exception as pv_error:
                logger.warning(f"⚠️ Could not calculate total page views: {pv_error}")
                total_page_views = 0

            logger.info(
                f"📊 Analytics Summary: {total_users_adjusted} total users, "
                f"{face_verified_total} face-verified ({verification_rate}), "
                f"{goodmarket_verified_users} via GoodMarket"
            )

            # GoodMarket claim attribution metrics (Version B table).
            # Prefer the service-role client when available so the read works
            # even under strict RLS where the anon role has no SELECT policy.
            # Safe fallback: if table doesn't exist yet, keep metrics at zero.
            gm_total_claims = 0
            gm_unique_claimers = 0
            try:
                gm_client = get_supabase_admin_client() or self.client
                total_claims_resp = gm_client.table("goodmarket_claim_facts")\
                    .select("tx_hash", count="exact")\
                    .eq("status", "confirmed")\
                    .execute()
                gm_total_claims = total_claims_resp.count if hasattr(total_claims_resp, 'count') else 0

                unique_claimers_resp = gm_client.table("goodmarket_claim_facts")\
                    .select("wallet_address")\
                    .eq("status", "confirmed")\
                    .execute()
                if unique_claimers_resp.data:
                    gm_unique_claimers = len({(r.get("wallet_address") or "").lower() for r in unique_claimers_resp.data if r.get("wallet_address")})
            except Exception as gm_err:
                logger.warning(f"⚠️ GoodMarket claim metrics unavailable: {gm_err}")

            return {
                "total_users": total_users_adjusted,
                "verified_users": verified_users,
                "face_verified_total": face_verified_total,
                "total_page_views": total_page_views,
                "verification_rate": verification_rate,
                "goodmarket_verified_users": goodmarket_verified_users,
                "pending_verification_users": pending_verification_users,
                "goodmarket_conversion_rate": goodmarket_conversion_rate,
                "goodmarket_total_claims": gm_total_claims,
                "goodmarket_unique_claimers": gm_unique_claimers
            }
        except Exception as e:
            logger.error(f"❌ Error getting analytics summary: {e}")
            return {
                "total_users": 0,
                "verified_users": 0,
                "total_page_views": 0,
                "verification_rate": "0%",
                "goodmarket_verified_users": 0,
                "pending_verification_users": 0,
                "goodmarket_conversion_rate": "0%",
                "goodmarket_total_claims": 0,
                "goodmarket_unique_claimers": 0
            }

    def get_ubi_statistics(self):
        """Get real UBI statistics from Supabase data"""
        try:
            from datetime import datetime, timedelta

            # Build a resilient verified-user total from legacy + current flags.
            verified_response = self.client.table("user_data").select("*", count="exact").eq("ubi_verified", True).execute()
            ubi_verified_count = (
                verified_response.count
                if hasattr(verified_response, 'count')
                else (len(verified_response.data) if verified_response.data else 0)
            )

            try:
                face_verified_response = self.client.table("user_data").select("*", count="exact").eq("face_verified", True).execute()
                face_verified_count = (
                    face_verified_response.count
                    if hasattr(face_verified_response, 'count')
                    else (len(face_verified_response.data) if face_verified_response.data else 0)
                )
            except Exception:
                face_verified_count = ubi_verified_count

            try:
                gm_verified_response = self.client.table("user_data").select("*", count="exact").eq("verified_after_goodmarket", True).execute()
                goodmarket_verified_count = (
                    gm_verified_response.count
                    if hasattr(gm_verified_response, 'count')
                    else (len(gm_verified_response.data) if gm_verified_response.data else 0)
                )
            except Exception:
                goodmarket_verified_count = 0

            total_verified = max(ubi_verified_count, face_verified_count, goodmarket_verified_count)

            # Get today's logins as proxy for active claims
            today = datetime.now().strftime("%Y-%m-%d")
            today_sessions_response = self.client.table("user_sessions").select("*", count="exact").gte("timestamp", today).execute()
            daily_activity = today_sessions_response.count if hasattr(today_sessions_response, 'count') else (len(today_sessions_response.data) if today_sessions_response.data else 0)

            # Calculate growth (compare with week ago)
            week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            week_sessions_response = self.client.table("user_sessions").select("*", count="exact").gte("timestamp", week_ago).execute()
            weekly_activity = week_sessions_response.count if hasattr(week_sessions_response, 'count') else (len(week_sessions_response.data) if week_sessions_response.data else 0)

            growth_rate = f"+{((daily_activity / max(weekly_activity/7, 1) - 1) * 100):.0f}% this week" if weekly_activity > 0 else "New platform"

            # Estimate daily pool based on verified users (typical UBI is ~100 G$ per user)
            estimated_daily_pool = total_verified * 100
            avg_claim = 100  # Typical G$ UBI amount

            return {
                "total_verified_users": f"{total_verified:,}",
                "daily_ubi_claims": f"~{daily_activity}",
                "growth_rate": growth_rate,
                "top_countries": ["Philippines", "Nigeria", "Brazil", "India", "Kenya"],  # This would need geo data
                "daily_pool_g": f"{estimated_daily_pool:,} G$",
                "avg_claim_amount": f"{avg_claim} G$",
                "claims_today": str(daily_activity)
            }
        except Exception as e:
            logger.error(f"❌ Error getting UBI statistics: {e}")
            return {
                "total_verified_users": "Error loading",
                "daily_ubi_claims": "Error loading", 
                "growth_rate": "Error loading",
                "top_countries": ["Error loading"],
                "daily_pool_g": "Error loading",
                "avg_claim_amount": "Error loading",
                "claims_today": "Error loading"
            }

    def _count_activities(self, sessions_data: list):
        """Helper function to count different activity types"""
        counts = {}
        for session in sessions_data:
            activity_type = session.get("activity_type", "unknown")
            counts[activity_type] = counts.get(activity_type, 0) + 1
        return counts

    def get_learn_earn_earnings(self, wallet_address: str) -> float:
        """Get total Learn & Earn earnings for a user"""
        try:
            if not self.enabled:
                return 0.0

            masked_wallet = self.mask_wallet_address(wallet_address)
            logger.info(f"🔍 Fetching Learn & Earn earnings for wallet: {masked_wallet}")

            # Fetch total earnings from the forum_pending_rewards table
            # The 'total_earned' column in this table should store the cumulative earnings.
            response = self.client.table("forum_pending_rewards")\
                .select("total_earned")\
                .eq("wallet_address", wallet_address)\
                .execute()

            if response.data and len(response.data) > 0:
                earnings = response.data[0].get("total_earned", 0.0)
                logger.info(f"✅ Found Learn & Earn earnings for {masked_wallet}: {earnings}")
                return float(earnings)
            else:
                logger.info(f"ℹ️ No Learn & Earn earnings found for {masked_wallet}. Assuming 0.")
                return 0.0

        except Exception as e:
            masked_wallet = self.mask_wallet_address(wallet_address) if wallet_address else "unknown"
            logger.error(f"❌ Error fetching Learn & Earn earnings for {masked_wallet}: {e}")
            return 0.0

def safe_supabase_operation(operation, fallback_result=None, operation_name="database operation"):
    """
    Safely execute a Supabase operation with error handling

    Args:
        operation: Lambda function containing the Supabase operation
        fallback_result: Value to return if operation fails
        operation_name: Name of the operation for logging

    Returns:
        Result of operation or fallback_result if it fails
    """
    try:
        return operation()
    except Exception as e:
        logger.error(f"❌ Error in {operation_name}: {e}")
        return fallback_result

def get_supabase_client(retries=3):
    """Get Supabase client instance with retry logic"""
    global supabase_enabled
    if supabase_enabled and supabase:
        return supabase

    # If client failed to initialize, try to reconnect
    if retries > 0 and SUPABASE_URL and SUPABASE_KEY:
        try:
            time.sleep(1)  # Brief delay before retry
            # Re-initialize the client
            return get_supabase_client(retries - 1)
        except:
            pass

    return None

def log_admin_action(admin_wallet: str, action_type: str, action_details: dict = None, target_wallet: str = None):
    """Log admin actions to database"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return

        from datetime import datetime
        action_data = {
            'admin_wallet': admin_wallet,
            'action_type': action_type,
            'action_details': action_details or {},
            'target_wallet': target_wallet,
            'created_at': datetime.utcnow().isoformat()
        }

        supabase.table('admin_actions_log').insert(action_data).execute()
        logger.info(f"✅ Logged admin action: {action_type} by {admin_wallet[:8]}...")
    except Exception as e:
        logger.error(f"❌ Error logging admin action: {e}")

def is_admin(wallet_address: str) -> bool:
    """Check if wallet address is an admin"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return False

        result = supabase.table('user_data')\
            .select('is_admin')\
            .eq('wallet_address', wallet_address)\
            .execute()

        if result.data and len(result.data) > 0:
            return result.data[0].get('is_admin', False)

        return False
    except Exception as e:
        logger.error(f"❌ Error checking admin status: {e}")
        return False

def set_admin_status(wallet_address: str, is_admin_status: bool) -> dict:
    """Set admin status for a user"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return {"success": False, "error": "Database not available"}

        result = supabase.table('user_data')\
            .update({'is_admin': is_admin_status})\
            .eq('wallet_address', wallet_address)\
            .execute()

        if result.data:
            logger.info(f"✅ Admin status set for {wallet_address[:8]}...: {is_admin_status}")
            return {"success": True}
        else:
            return {"success": False, "error": "User not found"}
    except Exception as e:
        logger.error(f"❌ Error setting admin status: {e}")
        return {"success": False, "error": str(e)}

# Global logger instance
supabase_logger = SupabaseLogger()
