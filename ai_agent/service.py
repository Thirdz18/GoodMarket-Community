"""Safe intent parsing and action planning for the GoodMarket AI agent.

The agent intentionally never signs or broadcasts transactions. It turns a
chat message into a validated preview that the frontend can show as a
confirmation card before existing wallet/payment flows are used.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

import requests
from web3 import Web3

from supabase_client import get_supabase_client, safe_supabase_operation

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = {
    "check_balance",
    "send_gd",
    "stream_gd",
    "mobile_load",
    "swap",
    "claim",
    "transaction_history",
    "help",
    "unknown",
}

AI_ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": sorted(SUPPORTED_ACTIONS)},
        "summary": {"type": "string"},
        "token": {"type": "string"},
        "amount": {"type": "string"},
        "fiat_amount": {"type": "string"},
        "fiat_currency": {"type": "string"},
        "recipient": {"type": "string"},
        "recipient_username": {"type": "string"},
        "phone": {"type": "string"},
        "from_token": {"type": "string"},
        "to_token": {"type": "string"},
        "flow_rate_per_day": {"type": "string"},
        "flow_rate_per_month": {"type": "string"},
        "requires_confirmation": {"type": "boolean"},
        "requires_signature": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "missing_fields": {"type": "array", "items": {"type": "string"}},
        "safety_note": {"type": "string"},
    },
    "required": [
        "action",
        "summary",
        "token",
        "amount",
        "fiat_amount",
        "fiat_currency",
        "recipient",
        "recipient_username",
        "phone",
        "from_token",
        "to_token",
        "flow_rate_per_day",
        "flow_rate_per_month",
        "requires_confirmation",
        "requires_signature",
        "confidence",
        "missing_fields",
        "safety_note",
    ],
}

_VALUE_ACTIONS = {"send_gd", "stream_gd", "mobile_load", "swap", "claim"}
_MAX_SEND_GD = Decimal(os.getenv("AI_AGENT_MAX_SEND_GD", "100"))
_MAX_STREAM_GD_PER_DAY = Decimal(os.getenv("AI_AGENT_MAX_STREAM_GD_PER_DAY", "100"))
_MAX_MOBILE_LOAD_FIAT = Decimal(os.getenv("AI_AGENT_MAX_MOBILE_LOAD_FIAT", "100"))
_ACTION_TTL_MINUTES = int(os.getenv("AI_AGENT_ACTION_TTL_MINUTES", "15"))
_OPENAI_MODEL = os.getenv("AI_AGENT_OPENAI_MODEL", "gpt-5.5-mini")

# Local fallback storage keeps the MVP functional without requiring a new DB
# migration. Production deployments can persist these rows in Supabase later.
_ACTION_STORE: dict[str, dict[str, Any]] = {}


@dataclass
class AgentResult:
    status: str
    reply: str
    intent: dict[str, Any]
    action: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "success": True,
            "status": self.status,
            "reply": self.reply,
            "intent": self.intent,
        }
        if self.action:
            payload["action"] = self.action
        return payload


def parse_and_plan(message: str, wallet: str | None, login_method: str | None) -> AgentResult:
    """Parse a chat message and create a safe action preview."""
    clean_message = (message or "").strip()
    if not clean_message:
        return AgentResult(
            status="needs_input",
            reply="Please type what you want to do, like 'send 10 G$ to 0x...' or 'load 09123456789 20'.",
            intent=_empty_intent("unknown", "No message provided."),
        )

    faq_reply = _faq_reply(clean_message)
    if faq_reply:
        return AgentResult(
            status="answer",
            reply=faq_reply,
            intent=_empty_intent("help", clean_message),
        )

    intent = _parse_with_openai(clean_message) or _parse_with_rules(clean_message)
    intent = _normalise_intent(intent)
    _supplement_intent_from_message(intent, clean_message)
    _apply_safety_policy(intent, wallet)

    if intent["action"] in _VALUE_ACTIONS and not wallet:
        intent["missing_fields"].append("wallet_session")
        intent["requires_confirmation"] = True
        intent["requires_signature"] = True
        return AgentResult(
            status="authentication_required",
            reply="Please connect and verify your GoodMarket wallet before preparing a transaction.",
            intent=intent,
        )

    if intent["missing_fields"]:
        return AgentResult(
            status="needs_details",
            reply=_missing_details_reply(intent),
            intent=intent,
        )

    if intent["action"] in _VALUE_ACTIONS:
        action = _create_pending_action(intent, wallet, login_method)
        return AgentResult(
            status="action_preview",
            reply="I prepared a safe preview. Please review and confirm before any wallet signing happens.",
            intent=intent,
            action=action,
        )

    return AgentResult(
        status="answer",
        reply=_read_only_reply(intent, wallet),
        intent=intent,
    )


def get_action(action_id: str, wallet: str | None) -> dict[str, Any] | None:
    action = _ACTION_STORE.get(str(action_id))
    if not action:
        return None
    if wallet and action.get("wallet_address") and action["wallet_address"].lower() != wallet.lower():
        return None
    return action


def confirm_action(action_id: str, wallet: str | None) -> dict[str, Any]:
    action = get_action(action_id, wallet)
    if not action:
        return {"success": False, "error": "Pending AI action not found."}
    if action.get("expires_at") and datetime.fromisoformat(action["expires_at"]) < datetime.now(timezone.utc):
        action["status"] = "expired"
        return {"success": False, "error": "This AI action has expired. Please create a new preview."}

    action["status"] = "confirmed"
    action["confirmed_at"] = datetime.now(timezone.utc).isoformat()

    signing_mode = "walletconnect" if (action.get("login_method") or "").lower() == "walletconnect" else "wallet"
    return {
        "success": True,
        "status": "signature_required",
        "message": "Action confirmed. Please continue with the existing GoodMarket wallet signing/payment flow.",
        "signing_mode": signing_mode,
        "action": action,
    }


def _parse_with_openai(message: str) -> dict[str, Any] | None:
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None

    prompt = (
        "You classify GoodMarket wallet chat commands. Return only schema-valid JSON. "
        "Never invent recipients, usernames, phone numbers, or amounts. For send_gd, accept either an EVM wallet address or a GoodMarket username as the recipient and preserve supported token symbols G$, GD, cUSD, or USDT. For stream_gd, extract the receiver and the G$ flow amount; use flow_rate_per_day when the user says per day/daily, otherwise place the numeric value in amount. Value-moving actions require confirmation and signature. "
        "Supported actions: check_balance, send_gd, stream_gd, mobile_load, swap, claim, transaction_history, help, unknown.\n\n"
        f"User message: {message}"
    )
    body = {
        "model": _OPENAI_MODEL,
        "input": prompt,
        "text": {
            "format": {
                "type": "json_schema",
                "name": "goodmarket_ai_intent",
                "strict": True,
                "schema": AI_ACTION_SCHEMA,
            }
        },
    }
    try:
        response = requests.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        text = data.get("output_text") or _extract_response_text(data)
        if text:
            return json.loads(text)
    except Exception as exc:  # noqa: BLE001 - fallback is intentional
        logger.warning("AI intent parse failed; using rule fallback: %s", exc)
    return None


def _extract_response_text(data: dict[str, Any]) -> str:
    chunks: list[str] = []
    for item in data.get("output", []) or []:
        for content in item.get("content", []) or []:
            if content.get("type") in {"output_text", "text"} and content.get("text"):
                chunks.append(content["text"])
    return "".join(chunks)


def _parse_with_rules(message: str) -> dict[str, Any]:
    lower = message.lower()
    intent = _empty_intent("unknown", "I can help prepare GoodMarket actions safely.")

    if any(word in lower for word in ["balance", "balanse", "how much", "magkano"]):
        intent.update(action="check_balance", summary="Check the connected wallet balance.", confidence=0.8)
        return intent
    if any(word in lower for word in ["history", "transactions", "tx", "recent"]):
        intent.update(action="transaction_history", summary="Show recent wallet transactions.", confidence=0.75)
        return intent
    if "claim" in lower:
        intent.update(action="claim", summary="Open or prepare a G$ claim action.", requires_confirmation=True, requires_signature=True, confidence=0.72)
        return intent
    if any(word in lower for word in ["swap", "palit", "exchange"]):
        amount = _first_amount(message)
        tokens = re.findall(r"(?<!\w)(g\$|gd|celo|cusd|usdt|xdc)(?!\w)", lower)
        intent.update(
            action="swap",
            summary="Prepare a token swap preview.",
            amount=amount or "",
            from_token=(tokens[0].upper() if tokens else ""),
            to_token=(tokens[1].upper() if len(tokens) > 1 else ""),
            requires_confirmation=True,
            requires_signature=True,
            confidence=0.7,
        )
        return intent
    if any(word in lower for word in ["load", "topup", "top up", "mobile"]):
        phone = _first_phone(message)
        amount = _mobile_load_amount(message, phone)
        intent.update(
            action="mobile_load",
            summary="Prepare a mobile load purchase preview.",
            phone=phone or "",
            fiat_amount=amount or "",
            fiat_currency="PHP",
            token=_send_token_candidate(message) or "G$",
            requires_confirmation=True,
            requires_signature=True,
            confidence=0.82,
        )
        return intent
    if "stream" in lower or "streaming" in lower:
        address = _first_address(message)
        amount = _first_amount(message)
        intent.update(
            action="stream_gd",
            summary="Prepare a G$ stream preview.",
            recipient=address or _send_recipient_candidate(message) or "",
            amount=amount or "",
            flow_rate_per_day=amount or "",
            token="G$",
            requires_confirmation=True,
            requires_signature=True,
            confidence=0.82,
        )
        return intent
    if any(word in lower for word in ["send", "transfer", "padala", "ipadala"]):
        address = _first_address(message)
        amount = _first_amount(message)
        intent.update(
            action="send_gd",
            summary="Prepare a G$ transfer preview.",
            recipient=address or _send_recipient_candidate(message) or "",
            amount=amount or "",
            token=_send_token_candidate(message) or "G$",
            requires_confirmation=True,
            requires_signature=True,
            confidence=0.82,
        )
        return intent
    if any(word in lower for word in ["help", "tulong", "what can you do"]):
        intent.update(action="help", summary="Explain what the GoodMarket AI agent can do.", confidence=0.9)
    return intent


def _normalise_intent(intent: dict[str, Any]) -> dict[str, Any]:
    normalised = _empty_intent(intent.get("action") if intent.get("action") in SUPPORTED_ACTIONS else "unknown", intent.get("summary") or "")
    normalised.update({k: str(intent.get(k) or "").strip() for k in ["token", "amount", "fiat_amount", "fiat_currency", "recipient", "recipient_username", "phone", "from_token", "to_token", "flow_rate_per_day", "flow_rate_per_month", "safety_note"]})
    normalised["requires_confirmation"] = bool(intent.get("requires_confirmation"))
    normalised["requires_signature"] = bool(intent.get("requires_signature"))
    try:
        normalised["confidence"] = max(0, min(1, float(intent.get("confidence", 0))))
    except (TypeError, ValueError):
        normalised["confidence"] = 0
    missing = intent.get("missing_fields") if isinstance(intent.get("missing_fields"), list) else []
    normalised["missing_fields"] = [str(field) for field in missing if str(field).strip()]
    return normalised


def _supplement_intent_from_message(intent: dict[str, Any], message: str) -> None:
    """Fill obvious fields that an LLM may omit; never invent values."""
    if intent["action"] == "mobile_load":
        phone = _first_phone(message)
        if phone and not intent.get("phone"):
            intent["phone"] = phone
        amount = _mobile_load_amount(message, phone)
        if amount and not intent.get("fiat_amount"):
            intent["fiat_amount"] = amount
    elif intent["action"] in {"send_gd", "stream_gd"}:
        address = _first_address(message)
        if address and not intent.get("recipient"):
            intent["recipient"] = address
        elif not intent.get("recipient"):
            username = _send_recipient_candidate(message)
            if username:
                intent["recipient"] = username
        amount = _first_amount(message)
        if amount and not intent.get("amount"):
            intent["amount"] = amount
        token = _send_token_candidate(message)
        if token:
            intent["token"] = token
        if intent["action"] == "stream_gd":
            daily = _stream_daily_amount(message)
            if daily and not intent.get("flow_rate_per_day"):
                intent["flow_rate_per_day"] = daily


def _apply_safety_policy(intent: dict[str, Any], wallet: str | None) -> None:
    action = intent["action"]
    intent["missing_fields"] = list(dict.fromkeys(intent.get("missing_fields", [])))

    if action == "send_gd":
        intent["token"] = _normalise_send_token(intent.get("token")) or "G$"
        if not _valid_decimal(intent["amount"]):
            intent["missing_fields"].append("amount")
        elif Decimal(intent["amount"]) > _MAX_SEND_GD:
            intent["missing_fields"].append(f"amount_at_or_below_{_MAX_SEND_GD}_{intent['token']}")
        _resolve_send_recipient(intent)
        if not intent["recipient"] or not Web3.is_address(intent["recipient"]):
            intent["missing_fields"].append("valid_recipient_username_or_wallet")
        intent["requires_confirmation"] = True
        intent["requires_signature"] = True
    elif action == "stream_gd":
        intent["token"] = "G$"
        if not intent.get("flow_rate_per_day") and intent.get("amount"):
            intent["flow_rate_per_day"] = intent["amount"]
        if not _valid_decimal(intent.get("flow_rate_per_day")):
            intent["missing_fields"].append("flow_rate_per_day")
        elif Decimal(intent["flow_rate_per_day"]) > _MAX_STREAM_GD_PER_DAY:
            intent["missing_fields"].append(f"flow_rate_at_or_below_{_MAX_STREAM_GD_PER_DAY}_G$_per_day")
        _resolve_send_recipient(intent)
        if not intent["recipient"] or not Web3.is_address(intent["recipient"]):
            intent["missing_fields"].append("valid_recipient_username_or_wallet")
        if _valid_decimal(intent.get("flow_rate_per_day")):
            intent["flow_rate_per_month"] = str(Decimal(intent["flow_rate_per_day"]) * Decimal("30"))
        intent["requires_confirmation"] = True
        intent["requires_signature"] = True
    elif action == "mobile_load":
        intent["token"] = intent["token"] or "G$"
        intent["fiat_currency"] = intent["fiat_currency"] or "PHP"
        if not _valid_phone(intent["phone"]):
            intent["missing_fields"].append("valid_phone_number")
        if not _valid_decimal(intent["fiat_amount"]):
            intent["missing_fields"].append("load_amount")
        elif Decimal(intent["fiat_amount"]) > _MAX_MOBILE_LOAD_FIAT:
            intent["missing_fields"].append(f"amount_at_or_below_{_MAX_MOBILE_LOAD_FIAT}_{intent['fiat_currency']}")
        intent["requires_confirmation"] = True
        intent["requires_signature"] = True
    elif action == "swap":
        if not _valid_decimal(intent["amount"]):
            intent["missing_fields"].append("amount")
        if not intent["from_token"]:
            intent["missing_fields"].append("from_token")
        if not intent["to_token"]:
            intent["missing_fields"].append("to_token")
        intent["requires_confirmation"] = True
        intent["requires_signature"] = True
    elif action == "claim":
        intent["requires_confirmation"] = True
        intent["requires_signature"] = True

    if action in _VALUE_ACTIONS and not wallet:
        intent["missing_fields"].append("connected_wallet")

    intent["missing_fields"] = list(dict.fromkeys(intent["missing_fields"]))
    if action in _VALUE_ACTIONS:
        intent["safety_note"] = "AI only prepares this action. The user must confirm and sign before anything executes."


def _create_pending_action(intent: dict[str, Any], wallet: str | None, login_method: str | None) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    action_id = str(uuid.uuid4())
    action = {
        "id": action_id,
        "wallet_address": wallet,
        "login_method": login_method or "",
        "action_type": intent["action"],
        "status": "pending_confirmation",
        "payload": intent,
        "requires_signature": intent["requires_signature"],
        "requires_confirmation": intent["requires_confirmation"],
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=_ACTION_TTL_MINUTES)).isoformat(),
    }
    _ACTION_STORE[action_id] = action
    return action


def _empty_intent(action: str, summary: str) -> dict[str, Any]:
    return {
        "action": action if action in SUPPORTED_ACTIONS else "unknown",
        "summary": summary,
        "token": "",
        "amount": "",
        "fiat_amount": "",
        "fiat_currency": "",
        "recipient": "",
        "recipient_username": "",
        "phone": "",
        "from_token": "",
        "to_token": "",
        "flow_rate_per_day": "",
        "flow_rate_per_month": "",
        "requires_confirmation": False,
        "requires_signature": False,
        "confidence": 0,
        "missing_fields": [],
        "safety_note": "",
    }


def _missing_details_reply(intent: dict[str, Any]) -> str:
    fields = ", ".join(intent.get("missing_fields", []))
    return f"I can help with that, but I need: {fields}."


def _read_only_reply(intent: dict[str, Any], wallet: str | None) -> str:
    action = intent["action"]
    if action == "check_balance":
        return _balance_reply(wallet)
    if action == "transaction_history":
        return "I can help show recent activity. This MVP can route you to the wallet transaction history without signing."
    if action == "help":
        return _welcome_help_reply()
    return _welcome_help_reply()




def _faq_reply(message: str) -> str | None:
    topic = re.sub(r"[^a-z0-9$& ]+", "", (message or "").lower()).strip()
    topic = re.sub(r"\s+", " ", topic)
    replies = {
        "what is goodmarket": (
            "GoodMarket is a Web3 earning platform in the GoodDollar ecosystem. "
            "Users can claim and use G$, complete learning/play tasks, use wallet tools, savings, P2P, and other GoodMarket features."
        ),
        "what is gooddollar": (
            "GoodDollar is the ecosystem behind G$, a Celo-based token focused on basic income and financial access. "
            "GoodMarket helps users discover and use GoodDollar features from one place."
        ),
        "what is goodmarket savings": (
            "GoodMarket Savings lets users lock or save G$ through the savings feature, depending on the active vault/settings. "
            "You still confirm any savings transaction in your wallet before funds move."
        ),
        "what is p2p trading": (
            "P2P trading means peer-to-peer trading: users can trade directly with another user, usually with an escrow or guided flow to make the exchange safer."
        ),
        "what is g$ stream": (
            "A G$ stream is a continuous G$ payment flow, such as 5 G$ per day, sent over time instead of as one instant transfer. "
            "The agent can prepare the stream preview, then your wallet must confirm and sign."
        ),
        "what is g stream": (
            "A G$ stream is a continuous G$ payment flow, such as 5 G$ per day, sent over time instead of as one instant transfer. "
            "The agent can prepare the stream preview, then your wallet must confirm and sign."
        ),
        "how to stream g$": _stream_gd_help_reply(),
        "how to stream g": _stream_gd_help_reply(),
        "how to stream gd": _stream_gd_help_reply(),
        "stream g$": _stream_gd_help_reply(),
        "what is play & earn": (
            "Play & Earn is GoodMarket's games/rewards area where users can complete supported games or activities and earn rewards when eligible."
        ),
        "what is play earn": (
            "Play & Earn is GoodMarket's games/rewards area where users can complete supported games or activities and earn rewards when eligible."
        ),
        "who is developer": (
            "GoodMarket is developed by the Betz & Omar Team. The app also shows developer/profile information where configured by the admins."
        ),
        "how to send tokens": _send_tokens_help_reply(),
        "how to send token": _send_tokens_help_reply(),
        "how to send g$ token": _send_tokens_help_reply(),
        "how to send gd token": _send_tokens_help_reply(),
        "send tokens": _send_tokens_help_reply(),
    }
    return replies.get(topic)

def _stream_gd_help_reply() -> str:
    return (
        "How to stream G$ inside GoodMarket Agent:\n"
        "1. Connect and verify your GoodMarket wallet first.\n"
        "2. Type a command like: stream 5 G$ per day to @username or stream 5 G$ per day to 0xWalletAddress.\n"
        "3. The agent prepares a review card showing the receiver and daily G$ flow rate. No stream starts yet.\n"
        "4. Check every detail, then tap Confirm action.\n"
        "5. Sign the wallet transaction to start the G$ stream.\n"
        "Tip: use a small daily amount first and make sure you have enough G$ and gas before confirming."
    )


def _send_tokens_help_reply() -> str:
    return (
        "How to send tokens with GoodMarket Agent:\n"
        "1. Type a command like: send 10 G$ to @username or send 10 G$ to 0xWalletAddress.\n"
        "2. You can also use cUSD or USDT, for example: send 1 cUSD to @username.\n"
        "3. The agent prepares a review card only. No token moves yet.\n"
        "4. Tap Confirm action, then sign in your wallet to actually send.\n"
        "Make sure the amount, token, and recipient are correct before confirming."
    )


def _welcome_help_reply() -> str:
    return (
        "Hello, welcome to GoodMarket! I can help with commands like:\n"
        "• check balance\n"
        "• send 10 G$ to @bebet or 0x wallet\n"
        "• send 1 cUSD to @bebet or 0x wallet\n"
        "• send 1 USDT to @bebet or 0x wallet\n"
        "• stream 5 G$ per day to @bebet or 0x wallet\n"
        "• load 09653870395 20\n"
        "For send and stream, username and wallet address are both supported. "
        "Value-moving actions stay in chat for review first, then require your confirmation and wallet signature."
    )

def _normalise_send_token(value: str | None) -> str | None:
    token = (value or "").strip().lower().replace(" ", "")
    aliases = {"g$": "G$", "gd": "G$", "gooddollar": "G$", "cusd": "cUSD", "celo-dollar": "cUSD", "usdt": "USDT", "tether": "USDT"}
    return aliases.get(token)

def _send_token_candidate(text: str) -> str | None:
    match = re.search(r"(?<!\w)(g\$|gd|gooddollar|cusd|celo-dollar|usdt|tether)(?!\w)", text, re.IGNORECASE)
    return _normalise_send_token(match.group(1)) if match else None

def _balance_reply(wallet: str | None) -> str:
    if not wallet:
        return "Please connect and verify your GoodMarket wallet so I can check your live balances."
    try:
        from concurrent.futures import ThreadPoolExecutor

        from blockchain import (
            get_celo_balance,
            get_cusd_balance,
            get_gooddollar_balance,
            get_usdt_balance,
        )

        balance_fetchers = {
            "G$": lambda: get_gooddollar_balance(wallet, include_price=False),
            "cUSD": lambda: get_cusd_balance(wallet),
            "USDT": lambda: get_usdt_balance(wallet),
            "CELO": lambda: get_celo_balance(wallet),
        }
        with ThreadPoolExecutor(max_workers=len(balance_fetchers)) as executor:
            futures = {symbol: executor.submit(fetcher) for symbol, fetcher in balance_fetchers.items()}
            results = {symbol: future.result() for symbol, future in futures.items()}
    except Exception as exc:  # noqa: BLE001 - user-facing fallback keeps chat usable
        logger.warning("AI balance check failed for %s: %s", wallet, exc)
        return "I could not load your live wallet balances right now. Please try again in a moment or open the wallet balance card."

    lines: list[str] = []
    for symbol in ["G$", "cUSD", "USDT", "CELO"]:
        result = results.get(symbol) or {}
        if result.get("success"):
            lines.append(f"• {result.get('balance_formatted') or _format_balance_amount(result.get('balance'), symbol)}")
        else:
            lines.append(f"• {symbol}: unavailable")

    return "Your live GoodMarket wallet balances are:\n" + "\n".join(lines)


def _resolve_send_recipient(intent: dict[str, Any]) -> None:
    recipient = (intent.get("recipient") or "").strip()
    if not recipient or Web3.is_address(recipient):
        return

    username = _normalise_username(recipient)
    if not username:
        return

    intent["recipient_username"] = username
    wallet = _lookup_wallet_by_username(username)
    if wallet:
        intent["recipient"] = Web3.to_checksum_address(wallet)


def _lookup_wallet_by_username(username: str) -> str | None:
    supabase = get_supabase_client()
    if not supabase:
        return None
    result = safe_supabase_operation(
        lambda: supabase.table("user_data")
            .select("wallet_address, username")
            .ilike("username", username)
            .limit(1)
            .execute(),
        operation_name="AI agent username recipient lookup",
    )
    if not result or not getattr(result, "data", None):
        return None
    wallet = (result.data[0].get("wallet_address") or "").strip()
    return wallet if Web3.is_address(wallet) else None


def _normalise_username(value: str) -> str | None:
    candidate = value.strip().lstrip("@").lower()
    return candidate if re.fullmatch(r"[a-z0-9_]{3,24}", candidate) else None


def _send_recipient_candidate(text: str) -> str | None:
    match = re.search(r"\b(?:to|kay|ni|user(?:name)?|@)\s*@?([A-Za-z0-9_]{3,24})\b", text, re.IGNORECASE)
    if not match:
        return None
    candidate = match.group(1)
    if candidate.lower() in {"wallet", "address", "goodmarket"}:
        return None
    return candidate


def _first_address(text: str) -> str | None:
    match = re.search(r"0x[a-fA-F0-9]{40}", text)
    return match.group(0) if match else None


def _first_phone(text: str) -> str | None:
    match = re.search(r"(?<!\d)(?:\+?63|0)?9\d{9}(?!\d)", text)
    return match.group(0) if match else None


def _first_amount(text: str) -> str | None:
    match = re.search(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])", text.replace(",", ""))
    return match.group(1) if match else None


def _mobile_load_amount(text: str, phone: str | None) -> str | None:
    scrubbed = text.replace(",", "")
    if phone:
        scrubbed = scrubbed.replace(phone, " ")
        if phone.startswith("0"):
            scrubbed = scrubbed.replace(phone[1:], " ")
    amounts = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])", scrubbed)
    if amounts:
        return amounts[-1]
    loose_amounts = re.findall(r"\d+(?:\.\d+)?", scrubbed)
    return loose_amounts[-1] if loose_amounts else None


def _valid_decimal(value: str) -> bool:
    try:
        return Decimal(value) > 0
    except (InvalidOperation, TypeError):
        return False


def _valid_phone(value: str) -> bool:
    return bool(value and re.fullmatch(r"(?:\+?63|0)?9\d{9}", value))


def _stream_daily_amount(text: str) -> str | None:
    """Return the requested G$/day amount when a stream command includes one."""
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:g\$|gd|gooddollar)?\s*(?:/|per\s+)?(?:day|daily|d)", text, re.IGNORECASE)
    if match:
        return match.group(1)
    return _first_amount(text)


def _format_balance_amount(value: Any, symbol: str) -> str:
    try:
        return f"{float(value or 0):,.6f} {symbol}"
    except (TypeError, ValueError):
        return f"0.000000 {symbol}"
