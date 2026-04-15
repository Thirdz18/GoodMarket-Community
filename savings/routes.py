import os
import logging
from flask import Blueprint, render_template, session, redirect, jsonify, request
from . import blockchain as svc

logger = logging.getLogger(__name__)

savings_bp = Blueprint("savings", __name__, url_prefix="/savings")

SAVINGS_CONTRACT_ADDRESS = os.getenv('SAVINGS_CONTRACT_ADDRESS', '')
GD_TOKEN_ADDRESS = os.getenv('GOODDOLLAR_CONTRACT_ADDRESS', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))


def _require_auth():
    wallet = session.get("wallet") or session.get("wallet_address")
    verified = session.get("verified") or session.get("ubi_verified")
    return wallet, verified


@savings_bp.route("/")
def savings_home():
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return redirect("/login")
    wc_pid = os.environ.get('WALLETCONNECT_PROJECT_ID', '')
    return render_template(
        "savings.html",
        wallet=wallet,
        savings_contract=SAVINGS_CONTRACT_ADDRESS,
        gd_contract=GD_TOKEN_ADDRESS,
        chain_id=CHAIN_ID,
        walletconnect_project_id=wc_pid,
        login_method=session.get("login_method", "walletconnect"),
    )


@savings_bp.route("/api/stats")
def api_stats():
    stats = svc.get_contract_stats()
    if not stats:
        return jsonify({"error": "Could not fetch stats"}), 500
    return jsonify(stats)


@savings_bp.route("/api/deposits")
def api_deposits():
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Unauthorized"}), 401
    deposits = svc.get_user_deposits(wallet)
    return jsonify({"deposits": deposits})


@savings_bp.route("/api/allowance")
def api_allowance():
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Unauthorized"}), 401
    allowance = svc.get_gd_allowance(wallet)
    return jsonify({"allowance": str(allowance)})
