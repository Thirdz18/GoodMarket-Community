"""
Telegram Bot Webhook Handler
Handles incoming Telegram bot updates, saves wallet-only Telegram logins,
and opens GoodMarket as a Mini App.
"""
import os
import json
import logging
import re
import secrets
import requests
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from flask import Blueprint, current_app, redirect, request, jsonify, session, url_for
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from config import PRODUCTION_DOMAIN
from supabase_client import get_supabase_admin_client, get_supabase_client

logger = logging.getLogger(__name__)

telegram_bot = Blueprint("telegram_bot", __name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_WEBHOOK_SECRET_TOKEN = os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "")
TELEGRAM_LOGIN_TOKEN_MAX_AGE_SECONDS = int(os.getenv("TELEGRAM_LOGIN_TOKEN_MAX_AGE_SECONDS", "900"))
_WALLET_RE = re.compile(r"^0x[a-fA-F0-9]{40}$")


def _normalize_base_url(url: str) -> str:
    """Normalize to scheme://host[:port] and remove paths/query/fragments."""
    raw_url = (url or "").strip()
    if not raw_url:
        return ""

    parsed = urlsplit(raw_url)

    # If env var is set without scheme, assume HTTPS.
    if not parsed.scheme:
        parsed = urlsplit(f"https://{raw_url}")

    return urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/")


APP_URL = _normalize_base_url(os.getenv("TELEGRAM_WEB_APP_URL", "") or PRODUCTION_DOMAIN)


def _normalize_wallet(wallet: str) -> str:
    """Return a normalized lowercase wallet address, or an empty string."""
    candidate = (wallet or "").strip()
    if not _WALLET_RE.match(candidate):
        return ""
    return candidate.lower()


def _mask_wallet(wallet: str) -> str:
    """Mask a wallet for Telegram messages."""
    normalized = _normalize_wallet(wallet)
    if not normalized:
        return ""
    return f"{normalized[:6]}…{normalized[-4:]}"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _login_serializer() -> URLSafeTimedSerializer:
    secret_key = current_app.secret_key or os.getenv("FLASK_SECRET_KEY") or os.getenv("SECRET_KEY")
    if not secret_key:
        secret_key = os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN") or TELEGRAM_BOT_TOKEN or "goodmarket-telegram-login"
    return URLSafeTimedSerializer(secret_key=secret_key, salt="telegram-learn-earn-login")


def _create_login_url(telegram_user_id: str, wallet: str) -> str:
    token = _login_serializer().dumps({
        "telegram_user_id": str(telegram_user_id),
        "wallet": _normalize_wallet(wallet),
        "nonce": secrets.token_urlsafe(8),
    })
    return f"{APP_URL}/telegram/learn-earn-login?token={token}"


def _get_saved_wallet(telegram_user_id) -> str:
    """Fetch a Telegram user's saved wallet from Supabase."""
    if not telegram_user_id:
        return ""
    try:
        supabase = get_supabase_admin_client() or get_supabase_client()
        if not supabase:
            return ""
        result = supabase.table("telegram_wallet_sessions")\
            .select("wallet_address")\
            .eq("telegram_user_id", str(telegram_user_id))\
            .limit(1)\
            .execute()
        if result.data:
            return _normalize_wallet(result.data[0].get("wallet_address", ""))
    except Exception as e:
        logger.error(f"❌ Could not fetch Telegram wallet session: {e}")
    return ""


def _save_wallet_session(telegram_user, chat_id, wallet: str) -> bool:
    """Persist a Telegram user → wallet mapping in Supabase."""
    normalized_wallet = _normalize_wallet(wallet)
    if not normalized_wallet:
        return False

    try:
        # Use the service-role client for server-side Telegram wallet capture so
        # Supabase RLS policies for browser/anon clients do not block the bot.
        # Fall back to the anon client for deployments that have not configured
        # SUPABASE_SERVICE_ROLE_KEY yet.
        supabase = get_supabase_admin_client() or get_supabase_client()
        if not supabase:
            logger.error("❌ Supabase unavailable; Telegram wallet session not saved")
            return False

        telegram_user_id = str(telegram_user.get("id", ""))
        now = _now_iso()
        row = {
            "telegram_user_id": telegram_user_id,
            "telegram_chat_id": str(chat_id),
            "username": telegram_user.get("username"),
            "first_name": telegram_user.get("first_name"),
            "last_name": telegram_user.get("last_name"),
            "wallet_address": normalized_wallet,
            "updated_at": now,
            "last_seen_at": now,
        }
        supabase.table("telegram_wallet_sessions")\
            .upsert(row, on_conflict="telegram_user_id")\
            .execute()

        # Best-effort user_data upsert keeps GoodMarket overview/profile counters aware
        # of wallet-only Telegram users without requiring WalletConnect. Do not
        # fail the Telegram wallet login if this optional profile sync fails
        # because the wallet session above is the source of truth for bot login.
        try:
            supabase.table("user_data")\
                .upsert({
                    "wallet_address": normalized_wallet,
                    "last_login": now,
                    "ubi_verified": False,
                    "login_method": "telegram_wallet",
                }, on_conflict="wallet_address")\
                .execute()
        except Exception as profile_error:
            logger.warning(f"⚠️ Telegram wallet saved but user_data sync failed: {profile_error}")

        return True
    except Exception as e:
        logger.error(f"❌ Could not save Telegram wallet session: {e}")
        return False


def _learn_earn_keyboard(telegram_user_id, wallet: str | None = None):
    saved_wallet = _normalize_wallet(wallet or "") or _get_saved_wallet(telegram_user_id)
    keyboard = []
    if saved_wallet:
        keyboard.append([{
            "text": "📚 Start Learn & Earn",
            "web_app": {"url": _create_login_url(telegram_user_id, saved_wallet)},
        }])
        keyboard.append([{"text": "💰 Open Wallet", "web_app": {"url": f"{APP_URL}/wallet"}}])
    keyboard.append([{"text": "🛒 Open GoodMarket", "web_app": {"url": APP_URL}}])
    return {"inline_keyboard": keyboard}


def send_message(chat_id, text, reply_markup=None):
    """Send a message to a Telegram chat."""
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)
    try:
        resp = requests.post(f"{TELEGRAM_API}/sendMessage", json=payload, timeout=10)
        return resp.json()
    except Exception as e:
        logger.error(f"Telegram sendMessage error: {e}")
        return None


def handle_start(chat_id, telegram_user):
    """Handle /start command — ask for wallet or open Learn & Earn."""
    first_name = telegram_user.get("first_name", "there")
    telegram_user_id = telegram_user.get("id")
    saved_wallet = _get_saved_wallet(telegram_user_id)

    if saved_wallet:
        text = (
            f"👋 Hello, <b>{first_name}</b>!\n\n"
            f"Your saved GoodMarket wallet is <code>{_mask_wallet(saved_wallet)}</code>.\n\n"
            "Tap <b>Start Learn & Earn</b> to continue without WalletConnect."
        )
        send_message(chat_id, text, _learn_earn_keyboard(telegram_user_id, saved_wallet))
        return

    text = (
        f"👋 Hello, <b>{first_name}</b>!\n\n"
        "Welcome to <b>GoodMarket Learn &amp; Earn</b> 📚\n\n"
        "Please send your wallet address here in Telegram.\n"
        "Example: <code>0x1234...abcd</code>\n\n"
        "This wallet will be saved as your GoodMarket login for Learn &amp; Earn, "
        "so no WalletConnect step is needed."
    )
    send_message(chat_id, text)


def handle_help(chat_id, telegram_user=None):
    """Handle /help command."""
    telegram_user_id = (telegram_user or {}).get("id")
    text = (
        "🤖 <b>GoodMarket Bot Commands</b>\n\n"
        "/start — Save your wallet or open Learn &amp; Earn\n"
        "/earn — Go to Learn &amp; Earn\n"
        "/wallet — Show your saved wallet\n"
        "/change_wallet — Replace your saved wallet\n"
        "/market — Open GoodMarket\n"
    )
    send_message(chat_id, text, _learn_earn_keyboard(telegram_user_id))


def handle_earn(chat_id, telegram_user):
    """Handle /earn command — open Learn & Earn when wallet is saved."""
    telegram_user_id = telegram_user.get("id")
    saved_wallet = _get_saved_wallet(telegram_user_id)
    if not saved_wallet:
        send_message(
            chat_id,
            "📚 <b>Learn &amp; Earn</b>\n\nPlease send your wallet address first so we can save your Learn &amp; Earn login.",
        )
        return

    text = (
        "📚 <b>Learn &amp; Earn is ready</b>\n\n"
        f"Saved wallet: <code>{_mask_wallet(saved_wallet)}</code>\n"
        "Modules will appear first, then the timed quiz questions from the admin dashboard."
    )
    send_message(chat_id, text, _learn_earn_keyboard(telegram_user_id, saved_wallet))


def handle_market(chat_id):
    """Handle /market command — open Marketplace page."""
    text = "🛒 <b>GoodMarket</b>\n\nOpen the marketplace inside Telegram."
    reply_markup = {
        "inline_keyboard": [
            [{"text": "🛒 Open GoodMarket", "web_app": {"url": APP_URL}}]
        ]
    }
    send_message(chat_id, text, reply_markup)


def handle_wallet(chat_id, telegram_user):
    """Handle /wallet command — show or request saved wallet."""
    telegram_user_id = telegram_user.get("id")
    saved_wallet = _get_saved_wallet(telegram_user_id)
    if not saved_wallet:
        send_message(chat_id, "💰 No wallet saved yet. Please send your wallet address now.")
        return

    text = (
        "💰 <b>Saved GoodMarket Wallet</b>\n\n"
        f"<code>{saved_wallet}</code>\n\n"
        "Send /change_wallet if you want to replace it."
    )
    send_message(chat_id, text, _learn_earn_keyboard(telegram_user_id, saved_wallet))


def handle_change_wallet(chat_id):
    """Prompt user to send a replacement wallet address."""
    send_message(
        chat_id,
        "🔁 <b>Change Wallet</b>\n\nSend the new wallet address you want to use for GoodMarket Learn &amp; Earn.",
    )


def handle_wallet_text(chat_id, telegram_user, text):
    """Treat non-command Telegram messages as wallet submissions."""
    wallet = _normalize_wallet(text)
    if not wallet:
        send_message(
            chat_id,
            "❌ That does not look like a valid wallet address. Please send a 42-character address that starts with <code>0x</code>.",
        )
        return

    if not _save_wallet_session(telegram_user, chat_id, wallet):
        logger.warning(
            "Telegram wallet DB save failed; sending signed temporary Learn & Earn login "
            f"for user {telegram_user.get('id')}"
        )
        text_msg = (
            "⚠️ <b>I could not permanently save your wallet yet.</b>\n\n"
            f"Wallet: <code>{_mask_wallet(wallet)}</code>\n\n"
            "You can still continue with this signed Telegram login button. "
            "If the bot asks for your wallet again later, the database save still needs to be fixed."
        )
        send_message(chat_id, text_msg, _learn_earn_keyboard(telegram_user.get("id"), wallet))
        return

    text_msg = (
        "✅ <b>Wallet saved!</b>\n\n"
        f"Wallet: <code>{_mask_wallet(wallet)}</code>\n\n"
        "You can now start Learn &amp; Earn without connecting a wallet. "
        "Your rewards and quiz history will use this wallet in GoodMarket Overview."
    )
    send_message(chat_id, text_msg, _learn_earn_keyboard(telegram_user.get("id"), wallet))


@telegram_bot.route("/telegram/learn-earn-login", methods=["GET"])
def telegram_learn_earn_login():
    """Convert a signed Telegram login token into a normal GoodMarket session."""
    token = request.args.get("token", "")
    if not token:
        return redirect(url_for("routes.index"))

    try:
        payload = _login_serializer().loads(token, max_age=TELEGRAM_LOGIN_TOKEN_MAX_AGE_SECONDS)
    except SignatureExpired:
        return "This Telegram login link has expired. Please go back to the bot and tap Learn & Earn again.", 410
    except BadSignature:
        return "Invalid Telegram login link.", 400

    wallet = _normalize_wallet(payload.get("wallet", ""))
    telegram_user_id = str(payload.get("telegram_user_id", ""))
    saved_wallet = _get_saved_wallet(telegram_user_id)
    if not wallet:
        return "Invalid Telegram login link.", 400
    if saved_wallet and saved_wallet != wallet:
        return "Telegram wallet session does not match this login link. Please save your wallet in the bot again.", 403
    if not saved_wallet:
        logger.warning(
            "Telegram Learn & Earn login proceeding from signed token without a saved DB row "
            f"for user {telegram_user_id}"
        )

    session["wallet_address"] = wallet
    session["wallet"] = wallet
    session["verified"] = True
    session["ubi_verified"] = False
    session["login_method"] = "telegram_wallet"
    session["telegram_user_id"] = telegram_user_id
    session.permanent = True
    session.modified = True

    return redirect("/learn-earn/")


@telegram_bot.route("/telegram/webhook", methods=["POST"])
def webhook():
    """Receive and handle Telegram updates."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return jsonify({"ok": False}), 500

    if TELEGRAM_WEBHOOK_SECRET_TOKEN:
        provided_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if provided_secret != TELEGRAM_WEBHOOK_SECRET_TOKEN:
            logger.warning("Rejected Telegram webhook: invalid secret token header")
            return jsonify({"ok": False, "error": "forbidden"}), 403

    update = request.get_json(silent=True)
    if not update:
        return jsonify({"ok": False}), 400

    try:
        message = update.get("message") or update.get("edited_message")
        callback = update.get("callback_query")

        if message:
            chat_id = message["chat"]["id"]
            telegram_user = message.get("from", {})
            text = message.get("text", "").strip()

            if text.startswith("/start"):
                handle_start(chat_id, telegram_user)
            elif text.startswith("/help"):
                handle_help(chat_id, telegram_user)
            elif text.startswith("/earn"):
                handle_earn(chat_id, telegram_user)
            elif text.startswith("/market"):
                handle_market(chat_id)
            elif text.startswith("/wallet"):
                handle_wallet(chat_id, telegram_user)
            elif text.startswith("/change_wallet"):
                handle_change_wallet(chat_id)
            else:
                handle_wallet_text(chat_id, telegram_user, text)

        if callback:
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": callback["id"]},
                timeout=5,
            )

    except Exception as e:
        logger.error(f"Telegram webhook error: {e}")

    return jsonify({"ok": True})


@telegram_bot.route("/telegram/setup-webhook", methods=["GET"])
def setup_webhook():
    """Register webhook URL with Telegram. Call this once after deploying."""
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"error": "TELEGRAM_BOT_TOKEN not set"}), 500

    webhook_url = f"{APP_URL}/telegram/webhook"
    resp = requests.post(
        f"{TELEGRAM_API}/setWebhook",
        json={
            "url": webhook_url,
            "allowed_updates": ["message", "callback_query"],
            "drop_pending_updates": True,
            **(
                {"secret_token": TELEGRAM_WEBHOOK_SECRET_TOKEN}
                if TELEGRAM_WEBHOOK_SECRET_TOKEN
                else {}
            ),
        },
        timeout=15,
    )
    result = resp.json()
    logger.info(f"Webhook setup result: {result}")
    return jsonify(result)


@telegram_bot.route("/telegram/webhook-info", methods=["GET"])
def webhook_info():
    """Check current webhook status."""
    if not TELEGRAM_BOT_TOKEN:
        return jsonify({"error": "TELEGRAM_BOT_TOKEN not set"}), 500
    resp = requests.get(f"{TELEGRAM_API}/getWebhookInfo", timeout=10)
    return jsonify(resp.json())
