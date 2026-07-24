"""
Telegram Bot Webhook Handler
Handles incoming Telegram bot updates, saves wallet-only Telegram logins,
and keeps Learn & Earn interactions inside the Telegram chat.
"""
import os
import asyncio
import html
import json
import logging
import re
import secrets
import time
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
_TELEGRAM_LEARN_EARN_SESSIONS = {}


def _run_async(coro):
    """Run an async Learn & Earn helper from the sync Telegram webhook."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("Event loop is closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


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


def _safe_text(value: str, limit: int = 700) -> str:
    """Convert module HTML or arbitrary text to Telegram-safe plain text."""
    text = re.sub(r"<[^>]+>", " ", value or "")
    text = html.unescape(re.sub(r"\s+", " ", text)).strip()
    if len(text) > limit:
        text = f"{text[:limit].rstrip()}…"
    return text


def _get_admin_dashboard_questions(quiz_manager):
    """Fetch quiz questions from the admin-managed Supabase question bank."""
    # Existing Learn & Earn behavior selects the quiz set from the Supabase
    # `quiz_questions` bank that admins manage in the dashboard. The questions
    # are not AI-generated and do not come from Telegram.
    return _run_async(quiz_manager.get_random_questions(quiz_manager.questions_per_quiz))


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
            "text": "📚 Start Learn & Earn chat",
            "callback_data": "learn_earn_chat",
        }])
        keyboard.append([{"text": "💰 Show saved wallet", "callback_data": "show_wallet"}])
    keyboard.append([{"text": "🛒 Open GoodMarket", "url": APP_URL}])
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


def _question_keyboard(question_number: int):
    return {
        "inline_keyboard": [
            [
                {"text": "A", "callback_data": f"le_ans:{question_number}:0"},
                {"text": "B", "callback_data": f"le_ans:{question_number}:1"},
                {"text": "C", "callback_data": f"le_ans:{question_number}:2"},
                {"text": "D", "callback_data": f"le_ans:{question_number}:3"},
            ]
        ]
    }


def _module_keyboard(module_index: int, is_last: bool):
    return {
        "inline_keyboard": [[{
            "text": "✅ Start quiz" if is_last else "➡️ Next module",
            "callback_data": f"le_mod_next:{module_index}",
        }]]
    }


def _start_questions_from_session(chat_id, telegram_user_id):
    session_data = _TELEGRAM_LEARN_EARN_SESSIONS.get(str(telegram_user_id))
    if not session_data:
        send_message(chat_id, "📚 No active Learn &amp; Earn chat quiz. Type /earn to start.")
        return

    session_data["phase"] = "quiz"
    session_data["current_index"] = 0
    send_message(
        chat_id,
        "📝 <b>Quiz starts now.</b>\n\n"
        f"You have <b>{int(session_data['time_per_question'])}s</b> per question. Tap A, B, C, or D.",
    )
    _send_current_question(chat_id, telegram_user_id)


def _send_current_module(chat_id, telegram_user_id):
    session_data = _TELEGRAM_LEARN_EARN_SESSIONS.get(str(telegram_user_id))
    if not session_data:
        send_message(chat_id, "📚 No active Learn &amp; Earn chat quiz. Type /earn to start.")
        return

    modules = session_data.get("modules") or []
    module_index = session_data.get("current_module_index", 0)
    if module_index >= len(modules):
        _start_questions_from_session(chat_id, telegram_user_id)
        return

    module = modules[module_index]
    title = html.escape(str(module.get("title") or f"Module {module_index + 1}"))
    reading_time = module.get("reading_time_minutes") or 1
    body = _safe_text(module.get("content") or module.get("description") or module.get("url") or "", limit=2200)
    if not body:
        body = "No module body was provided yet, but this module is active in the admin dashboard."
    is_last = module_index == len(modules) - 1
    text = (
        f"📘 <b>Module {module_index + 1}/{len(modules)}: {title}</b>\n"
        f"Estimated reading time: <b>{reading_time} min</b>\n\n"
        f"{html.escape(body)}\n\n"
        "Read this module, then tap the button below."
    )
    send_message(chat_id, text, _module_keyboard(module_index, is_last))


def _send_current_question(chat_id, telegram_user_id):
    session_data = _TELEGRAM_LEARN_EARN_SESSIONS.get(str(telegram_user_id))
    if not session_data:
        send_message(chat_id, "📚 No active Learn &amp; Earn chat quiz. Type /earn to start.")
        return
    if session_data.get("phase") != "quiz":
        send_message(chat_id, "📘 Please finish the module step first, then the quiz will start.")
        return

    current_index = session_data["current_index"]
    questions = session_data["questions"]
    question = questions[current_index]
    seconds = int(session_data["time_per_question"])
    session_data["deadline"] = time.time() + seconds

    options = question.get("options", [])
    option_lines = "\n".join(
        f"{chr(65 + idx)}. {html.escape(str(option))}"
        for idx, option in enumerate(options[:4])
    )
    text = (
        f"⏱️ <b>Question {current_index + 1}/{len(questions)}</b> — {seconds}s timer\n\n"
        f"{html.escape(str(question.get('question', '')))}\n\n"
        f"{option_lines}\n\n"
        "Tap A, B, C, or D."
    )
    send_message(chat_id, text, _question_keyboard(current_index))


def _finish_chat_quiz(chat_id, telegram_user_id):
    from learn_and_earn.learn_and_earn import quiz_manager

    session_key = str(telegram_user_id)
    session_data = _TELEGRAM_LEARN_EARN_SESSIONS.pop(session_key, None)
    if not session_data:
        send_message(chat_id, "📚 No active Learn &amp; Earn chat quiz. Type /earn to start.")
        return

    quiz_result = quiz_manager.validate_and_score_quiz(
        session_data["quiz_session_id"],
        session_data["answers"],
    )
    if not quiz_result.get("valid"):
        send_message(chat_id, f"⚠️ {html.escape(quiz_result.get('message', 'Quiz could not be scored.'))}")
        return

    wallet = session_data["wallet"]
    reward_amount = quiz_result.get("reward_amount", 0)
    try:
        _run_async(quiz_manager.save_quiz_attempt(
            wallet,
            quiz_result.get("questions", session_data["questions"]),
            session_data["answers"],
            reward_amount,
            {"verified": False, "source": "telegram_chat"},
        ))
    except Exception as save_error:
        logger.error(f"❌ Telegram chat quiz save failed: {save_error}")

    send_message(
        chat_id,
        "✅ <b>Learn &amp; Earn chat quiz complete!</b>\n\n"
        f"Score: <b>{quiz_result.get('score')}/{quiz_result.get('total_questions')}</b>\n"
        f"Reward earned: <b>{reward_amount} G$</b>\n\n"
        "Your quiz attempt was recorded for your saved wallet. Type /earn to start again when eligible.",
    )


def handle_learn_earn_answer(chat_id, telegram_user_id, callback_data: str):
    parts = callback_data.split(":")
    if len(parts) != 3:
        return

    session_data = _TELEGRAM_LEARN_EARN_SESSIONS.get(str(telegram_user_id))
    if not session_data:
        send_message(chat_id, "📚 No active Learn &amp; Earn chat quiz. Type /earn to start.")
        return

    try:
        question_index = int(parts[1])
        answer_index = int(parts[2])
    except ValueError:
        return

    if question_index != session_data["current_index"]:
        send_message(chat_id, "ℹ️ That answer is for an old question. Please answer the latest question.")
        return

    timed_out = time.time() > session_data.get("deadline", 0)
    if timed_out:
        selected_answer = -1
        send_message(chat_id, "⏱️ Time is up for that question. Marked as incorrect.")
    else:
        selected_answer = answer_index
        send_message(chat_id, f"✅ Answer {chr(65 + answer_index)} received.")

    session_data["answers"].append(selected_answer)
    session_data["current_index"] += 1

    if session_data["current_index"] >= len(session_data["questions"]):
        _finish_chat_quiz(chat_id, telegram_user_id)
    else:
        _send_current_question(chat_id, telegram_user_id)


def handle_learn_earn_module_next(chat_id, telegram_user_id, callback_data: str):
    parts = callback_data.split(":")
    if len(parts) != 2:
        return

    session_data = _TELEGRAM_LEARN_EARN_SESSIONS.get(str(telegram_user_id))
    if not session_data:
        send_message(chat_id, "📚 No active Learn &amp; Earn chat quiz. Type /earn to start.")
        return
    if session_data.get("phase") != "module":
        send_message(chat_id, "📝 The quiz has already started. Please answer the current question.")
        return

    try:
        module_index = int(parts[1])
    except ValueError:
        return

    if module_index != session_data.get("current_module_index", 0):
        send_message(chat_id, "ℹ️ That module button is old. Please use the latest module message.")
        return

    session_data["current_module_index"] = module_index + 1
    if session_data["current_module_index"] >= len(session_data.get("modules") or []):
        _start_questions_from_session(chat_id, telegram_user_id)
    else:
        _send_current_module(chat_id, telegram_user_id)


def handle_start(chat_id, telegram_user):
    """Handle /start command — ask for wallet or open Learn & Earn."""
    first_name = telegram_user.get("first_name", "there")
    telegram_user_id = telegram_user.get("id")
    saved_wallet = _get_saved_wallet(telegram_user_id)

    if saved_wallet:
        text = (
            f"👋 Hello, <b>{first_name}</b>!\n\n"
            f"Your saved GoodMarket wallet is <code>{_mask_wallet(saved_wallet)}</code>.\n\n"
            "Tap <b>Start Learn & Earn chat</b> to continue here in Telegram without Mini App or WalletConnect."
        )
        send_message(chat_id, text, _learn_earn_keyboard(telegram_user_id, saved_wallet))
        return

    text = (
        f"👋 Hello, <b>{first_name}</b>!\n\n"
        "Welcome to <b>GoodMarket Learn &amp; Earn</b> 📚\n\n"
        "Please send your wallet address here in Telegram.\n"
        "Example: <code>0x1234...abcd</code>\n\n"
        "This wallet will be saved for chat-based Learn &amp; Earn, "
        "so no Mini App or WalletConnect step is needed."
    )
    send_message(chat_id, text)


def handle_help(chat_id, telegram_user=None):
    """Handle /help command."""
    telegram_user_id = (telegram_user or {}).get("id")
    text = (
        "🤖 <b>GoodMarket Bot Commands</b>\n\n"
        "/start — Save your wallet or open Learn &amp; Earn\n"
        "/earn — Start Learn &amp; Earn in this chat\n"
        "/wallet — Show your saved wallet\n"
        "/change_wallet — Replace your saved wallet\n"
        "/market — Open GoodMarket\n"
    )
    send_message(chat_id, text, _learn_earn_keyboard(telegram_user_id))


def handle_earn(chat_id, telegram_user):
    """Handle /earn command — start the chat-first Learn & Earn flow when wallet is saved."""
    from learn_and_earn.learn_and_earn import quiz_manager

    telegram_user_id = telegram_user.get("id")
    saved_wallet = _get_saved_wallet(telegram_user_id)
    if not saved_wallet:
        send_message(
            chat_id,
            "📚 <b>Learn &amp; Earn</b>\n\nPlease send your wallet address first so we can save your Learn &amp; Earn login.",
        )
        return

    try:
        eligibility = _run_async(quiz_manager.check_quiz_eligibility(saved_wallet))
        if not eligibility.get("eligible", True):
            send_message(
                chat_id,
                "⏳ <b>Learn &amp; Earn is not available yet</b>\n\n"
                f"{html.escape(str(eligibility.get('message', 'Please try again later.')))}",
                _learn_earn_keyboard(telegram_user_id, saved_wallet),
            )
            return

        modules = quiz_manager.get_module_links()
        questions = _get_admin_dashboard_questions(quiz_manager)
        if not questions:
            send_message(chat_id, "⚠️ No Learn &amp; Earn quiz questions are available right now. Please try again later.")
            return

        quiz_session = quiz_manager.create_quiz_session(saved_wallet, questions)
        _TELEGRAM_LEARN_EARN_SESSIONS[str(telegram_user_id)] = {
            "wallet": saved_wallet,
            "quiz_session_id": quiz_session["session_id"],
            "modules": modules,
            "questions": questions,
            "phase": "module" if modules else "quiz",
            "current_module_index": 0,
            "answers": [],
            "current_index": 0,
            "time_per_question": quiz_manager.time_per_question,
            "deadline": 0,
        }
    except Exception as e:
        logger.error(f"❌ Telegram Learn & Earn chat start failed: {e}")
        send_message(chat_id, "⚠️ Learn &amp; Earn chat quiz could not start. Please try again later.")
        return

    text = (
        "📚 <b>Learn &amp; Earn chat quiz started</b>\n\n"
        f"Saved wallet: <code>{_mask_wallet(saved_wallet)}</code>\n"
        f"Source: active modules from <code>learn_earn_module_links</code> and admin-dashboard questions from <code>quiz_questions</code>.\n"
        f"Timer: <b>{quiz_manager.time_per_question}s per question</b>.\n\n"
        + (
            f"You have <b>{len(modules)}</b> module(s) to read first. The quiz starts after the module step."
            if modules
            else "No active module is available right now, so the quiz starts immediately."
        )
    )
    send_message(chat_id, text)
    if modules:
        _send_current_module(chat_id, telegram_user_id)
    else:
        _start_questions_from_session(chat_id, telegram_user_id)


def handle_market(chat_id):
    """Handle /market command — open Marketplace page."""
    text = "🛒 <b>GoodMarket</b>\n\nOpen the marketplace from Telegram."
    reply_markup = {
        "inline_keyboard": [
            [{"text": "🛒 Open GoodMarket", "url": APP_URL}]
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
        "You can now start Learn &amp; Earn directly in this Telegram chat without opening a Mini App or connecting a wallet. "
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
            callback_user = callback.get("from", {})
            callback_chat_id = (callback.get("message") or {}).get("chat", {}).get("id")
            callback_data = callback.get("data", "")
            requests.post(
                f"{TELEGRAM_API}/answerCallbackQuery",
                json={"callback_query_id": callback["id"]},
                timeout=5,
            )
            if callback_chat_id and callback_data == "learn_earn_chat":
                handle_earn(callback_chat_id, callback_user)
            elif callback_chat_id and callback_data == "show_wallet":
                handle_wallet(callback_chat_id, callback_user)
            elif callback_chat_id and callback_data.startswith("le_mod_next:"):
                handle_learn_earn_module_next(callback_chat_id, callback_user.get("id"), callback_data)
            elif callback_chat_id and callback_data.startswith("le_ans:"):
                handle_learn_earn_answer(callback_chat_id, callback_user.get("id"), callback_data)

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
