"""HTTP routes for the GoodMarket AI chat agent."""

from __future__ import annotations

from flask import Blueprint, jsonify, request, session

from .service import confirm_action, get_action, parse_and_plan

ai_agent_bp = Blueprint("ai_agent", __name__, url_prefix="/api/ai-agent")


@ai_agent_bp.route("/chat", methods=["POST"])
def ai_agent_chat():
    data = request.get_json(silent=True) or {}
    message = data.get("message", "")
    wallet = session.get("wallet") or session.get("wallet_address")
    login_method = session.get("login_method", "")
    result = parse_and_plan(message=message, wallet=wallet, login_method=login_method)
    return jsonify(result.to_dict())


@ai_agent_bp.route("/actions/<action_id>", methods=["GET"])
def ai_agent_get_action(action_id: str):
    wallet = session.get("wallet") or session.get("wallet_address")
    action = get_action(action_id, wallet)
    if not action:
        return jsonify({"success": False, "error": "AI action not found"}), 404
    return jsonify({"success": True, "action": action})


@ai_agent_bp.route("/actions/<action_id>/confirm", methods=["POST"])
def ai_agent_confirm_action(action_id: str):
    wallet = session.get("wallet") or session.get("wallet_address")
    result = confirm_action(action_id, wallet)
    return jsonify(result), (200 if result.get("success") else 400)
