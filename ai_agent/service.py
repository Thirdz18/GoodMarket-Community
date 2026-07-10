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

logger = logging.getLogger(__name__)

SUPPORTED_ACTIONS = {
    "check_balance",
    "send_gd",
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
        "phone": {"type": "string"},
        "from_token": {"type": "string"},
        "to_token": {"type": "string"},
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
        "phone",
        "from_token",
        "to_token",
        "requires_confirmation",
        "requires_signature",
        "confidence",
        "missing_fields",
        "safety_note",
    ],
}

_VALUE_ACTIONS = {"send_gd", "mobile_load", "swap", "claim"}
_MAX_SEND_GD = Decimal(os.getenv("AI_AGENT_MAX_SEND_GD", "100"))
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
        "Never invent recipients, phone numbers, or amounts. Value-moving actions require confirmation and signature. "
        "Supported actions: check_balance, send_gd, mobile_load, swap, claim, transaction_history, help, unknown.\n\n"
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
        tokens = re.findall(r"\b(g\$|gd|celo|cusd|xdc)\b", lower)
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
            recipient=address or "",
            amount=amount or "",
            token="G$",
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
    normalised.update({k: str(intent.get(k) or "").strip() for k in ["token", "amount", "fiat_amount", "fiat_currency", "recipient", "phone", "from_token", "to_token", "safety_note"]})
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
    elif intent["action"] == "send_gd":
        address = _first_address(message)
        if address and not intent.get("recipient"):
            intent["recipient"] = address
        amount = _first_amount(message)
        if amount and not intent.get("amount"):
            intent["amount"] = amount


def _apply_safety_policy(intent: dict[str, Any], wallet: str | None) -> None:
    action = intent["action"]
    intent["missing_fields"] = list(dict.fromkeys(intent.get("missing_fields", [])))

    if action == "send_gd":
        intent["token"] = intent["token"] or "G$"
        if not _valid_decimal(intent["amount"]):
            intent["missing_fields"].append("amount")
        elif Decimal(intent["amount"]) > _MAX_SEND_GD:
            intent["missing_fields"].append(f"amount_at_or_below_{_MAX_SEND_GD}_G$")
        if not intent["recipient"] or not Web3.is_address(intent["recipient"]):
            intent["missing_fields"].append("valid_recipient_wallet")
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
        "phone": "",
        "from_token": "",
        "to_token": "",
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
        return "You can ask me to prepare G$ sends, mobile load purchases, swaps, claims, balance checks, or transaction history. Value-moving actions always require confirmation and wallet signing."
    return "I am not sure yet. Try: 'send 10 G$ to 0x...', 'load 09123456789 20', or 'check balance'."


def _balance_reply(wallet: str | None) -> str:
    if not wallet:
        return "Please connect and verify your GoodMarket wallet so I can check your live G$ balance."
    try:
        from blockchain import get_gooddollar_balance

        result = get_gooddollar_balance(wallet, include_price=True)
    except Exception as exc:  # noqa: BLE001 - user-facing fallback keeps chat usable
        logger.warning("AI balance check failed for %s: %s", wallet, exc)
        return "I could not load your live G$ balance right now. Please try again in a moment or open the wallet balance card."

    if not result or not result.get("success"):
        return "I could not load your live G$ balance right now. Please try again in a moment or open the wallet balance card."

    balance = result.get("balance_formatted") or f"{result.get('balance', 0):,.6f} G$"
    usd = result.get("usd_formatted")
    if usd:
        return f"Your live GoodDollar balance is {balance} ({usd})."
    return f"Your live GoodDollar balance is {balance}."


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
