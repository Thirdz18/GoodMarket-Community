import os
import logging
from flask import Blueprint, render_template, session, redirect, jsonify, request
from . import blockchain as svc

logger = logging.getLogger(__name__)

savings_bp = Blueprint("savings", __name__, url_prefix="/savings")

SAVINGS_CONTRACT_ADDRESS = os.getenv('SAVINGS_CONTRACT_ADDRESS', '')
GD_TOKEN_ADDRESS = os.getenv('GOODDOLLAR_CONTRACT_ADDRESS', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
CELO_TOKEN_ADDRESS = os.getenv('CELO_TOKEN_ADDRESS', '0x471EcE3750Da237f93B8E339c536989b8978a438')
CUSD_TOKEN_ADDRESS = os.getenv('CUSD_TOKEN_ADDRESS', '0x765DE816845861e75A25fCA122bb6898B8B1282a')
USDT_TOKEN_ADDRESS = svc.USDT_TOKEN_ADDRESS
CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))
LEGACY_V2_CONTRACT_ADDRESS = svc.LEGACY_V2_CONTRACT_ADDRESS
LEGACY_V4_CONTRACT_ADDRESS = svc.LEGACY_V4_CONTRACT_ADDRESS


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
    has_explicit_sidecar = bool(os.getenv("WC_SERVICE_URL"))
    is_serverless_runtime = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
    wc_sidecar = has_explicit_sidecar or not is_serverless_runtime
    return render_template(
        "savings.html",
        wallet=wallet,
        savings_contract=SAVINGS_CONTRACT_ADDRESS,
        gd_contract=GD_TOKEN_ADDRESS,
        celo_contract=CELO_TOKEN_ADDRESS,
        cusd_contract=CUSD_TOKEN_ADDRESS,
        usdt_contract=USDT_TOKEN_ADDRESS,
        legacy_v2_contract=LEGACY_V2_CONTRACT_ADDRESS,
        legacy_v4_contract=LEGACY_V4_CONTRACT_ADDRESS,
        chain_id=CHAIN_ID,
        walletconnect_project_id=wc_pid,
        walletconnect_sidecar_enabled=wc_sidecar,
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
    """Backwards-compatible: G$ allowance only."""
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Unauthorized"}), 401
    allowance = svc.get_gd_allowance(wallet)
    return jsonify({"allowance": str(allowance)})


@savings_bp.route("/api/balances")
def api_balances():
    """Per-token balances + allowances (G$, CELO, cUSD) for the connected wallet."""
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Unauthorized"}), 401
    return jsonify({"balances": svc.get_user_token_balances(wallet)})


@savings_bp.route("/api/token-allowance")
def api_token_allowance():
    """Allowance for a specific token (?token=0x...)."""
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Unauthorized"}), 401
    token = request.args.get("token", "")
    if not token:
        return jsonify({"error": "Missing token query parameter"}), 400
    return jsonify({"allowance": str(svc.get_token_allowance(wallet, token))})


@savings_bp.route("/api/legacy-deposits")
def api_legacy_deposits():
    """Read-only list of v2 deposits for the connected wallet on the
    frozen legacy contract. Returns an empty array if the user never
    interacted with v2 — the frontend hides the panel in that case."""
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Unauthorized"}), 401
    deposits = svc.get_user_legacy_deposits(wallet)
    return jsonify({
        "contract": LEGACY_V2_CONTRACT_ADDRESS,
        "deposits": deposits,
    })


@savings_bp.route("/api/legacy-v4-deposits")
def api_legacy_v4_deposits():
    """Read-only list of active v4 slots for the connected wallet on the
    pre-v5 multi-token savings contract. Same shape as `/api/deposits`,
    so the frontend can reuse its row-rendering logic. Returns an empty
    array if the user has no active v4 slots — the frontend hides the
    legacy v4 panel in that case."""
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Unauthorized"}), 401
    deposits = svc.get_user_legacy_v4_deposits(wallet)
    return jsonify({
        "contract": LEGACY_V4_CONTRACT_ADDRESS,
        "deposits": deposits,
    })
