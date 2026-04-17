"""
Telegram Bot Webhook Handler
Handles incoming Telegram bot updates and opens GoodMarket as a Mini App.
"""
import os
import json
import logging
import requests
from urllib.parse import urlsplit, urlunsplit
from flask import Blueprint, request, jsonify
from config import PRODUCTION_DOMAIN

logger = logging.getLogger(__name__)

telegram_bot = Blueprint("telegram_bot", __name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_API = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"
TELEGRAM_WEBHOOK_SECRET_TOKEN = os.getenv("TELEGRAM_WEBHOOK_SECRET_TOKEN", "")


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


def handle_start(chat_id, first_name):
    """Handle /start command — send welcome + Mini App button."""
    text = (
        f"👋 Hello, <b>{first_name}</b>!\n\n"
        f"Welcome to <b>GoodMarket</b> 🌍\n\n"
        f"📚 Learn &amp; Earn with GoodDollar (G$)\n"
        f"🛒 P2P Marketplace\n"
        f"🎮 Mini Games\n"
        f"💰 Savings &amp; Rewards\n\n"
        f"Tap the button below to open the app:"
    )
    reply_markup = {
        "inline_keyboard": [
            [
                {
                    "text": "🚀 Open GoodMarket",
                    "web_app": {"url": APP_URL}
                }
            ],
            [
                {
                    "text": "📖 Learn & Earn",
                    "web_app": {"url": f"{APP_URL}/dashboard"}
                }
            ]
        ]
    }
    send_message(chat_id, text, reply_markup)


def handle_help(chat_id):
    """Handle /help command."""
    text = (
        "🤖 <b>GoodMarket Bot Commands</b>\n\n"
        "/start — Open GoodMarket Mini App\n"
        "/help — Show this help message\n"
        "/earn — Go to Learn &amp; Earn\n"
        "/market — Go to P2P Marketplace\n"
        "/wallet — Go to Wallet\n"
    )
    reply_markup = {
        "inline_keyboard": [
            [{"text": "🚀 Open GoodMarket", "web_app": {"url": APP_URL}}]
        ]
    }
    send_message(chat_id, text, reply_markup)


def handle_earn(chat_id):
    """Handle /earn command — open Learn & Earn page."""
    text = "📚 <b>Learn &amp; Earn</b>\n\nComplete quizzes and earn G$ rewards!"
    reply_markup = {
        "inline_keyboard": [
            [{"text": "📚 Open Learn & Earn", "web_app": {"url": f"{APP_URL}/dashboard"}}]
        ]
    }
    send_message(chat_id, text, reply_markup)


def handle_market(chat_id):
    """Handle /market command — open Marketplace page."""
    text = "🛒 <b>P2P Marketplace</b>\n\nBuy and sell using G$ tokens!"
    reply_markup = {
        "inline_keyboard": [
            [{"text": "🛒 Open Marketplace", "web_app": {"url": f"{APP_URL}/dashboard"}}]
        ]
    }
    send_message(chat_id, text, reply_markup)


def handle_wallet(chat_id):
    """Handle /wallet command — open Wallet page."""
    text = "💰 <b>Wallet</b>\n\nCheck your G$ balance and transactions."
    reply_markup = {
        "inline_keyboard": [
            [{"text": "💰 Open Wallet", "web_app": {"url": f"{APP_URL}/wallet"}}]
        ]
    }
    send_message(chat_id, text, reply_markup)


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
        message  = update.get("message") or update.get("edited_message")
        callback = update.get("callback_query")

        if message:
            chat_id    = message["chat"]["id"]
            first_name = message.get("from", {}).get("first_name", "there")
            text       = message.get("text", "").strip()

            if text.startswith("/start"):
                handle_start(chat_id, first_name)
            elif text.startswith("/help"):
                handle_help(chat_id)
            elif text.startswith("/earn"):
                handle_earn(chat_id)
            elif text.startswith("/market"):
                handle_market(chat_id)
            elif text.startswith("/wallet"):
                handle_wallet(chat_id)
            else:
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "🚀 Open GoodMarket", "web_app": {"url": APP_URL}}]
                    ]
                }
                send_message(
                    chat_id,
                    "Tap the button below to open GoodMarket 👇",
                    reply_markup
                )

        if callback:
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": callback["id"]},
                timeout=5
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
            )
        },
        timeout=15
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
