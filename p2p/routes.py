"""
P2P G$ trading routes.

Conventions mirror the savings module:
  - Page route renders the template with the contract address, chain id, and
    `login_method` so the frontend can sign with the right provider
    (injected / WalletConnect / GoodMarket) via GMWalletConnect.
  - Normal buyer/seller fund txs are USER-SIGNED in the browser; the backend
    only records the resulting offchain state (and verifies against chain).
  - Admin-review release/refund (seller rejected the proof) is SERVER-SIGNED
    with P2P_KEY and lives under /p2p/api/admin/* (admin dashboard only).
"""
import os
import time
import logging
import threading
from collections import defaultdict, deque
from functools import wraps

from flask import Blueprint, render_template, session, redirect, jsonify, request

from env_utils import get_env_int

from . import blockchain as chain
from . import db

logger = logging.getLogger(__name__)

p2p_bp = Blueprint("p2p", __name__, url_prefix="/p2p")

GD_TOKEN_ADDRESS = chain.GD_TOKEN_ADDRESS
CHAIN_ID = chain.CHAIN_ID

# Order statuses the admin dashboard must review.
REVIEW_STATUSES = ["seller_rejected", "disputed"]


def feature_enabled():
    """Kill-switch for the whole P2P surface. Defaults ON; set P2P_ENABLED=0 to
    hide the page and reject write endpoints (read endpoints still degrade)."""
    return os.getenv("P2P_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")


# ── Lightweight in-process rate limiter ──────────────────────────────────────
# No external dependency (the repo has none). Per-(wallet-or-ip, bucket) sliding
# window. Best-effort per Gunicorn worker — enough to blunt abusive bursts of
# writes (listing/order/proof/chat) without a shared store.
_RL_LOCK = threading.Lock()
_RL_HITS = defaultdict(deque)


def rate_limit(max_calls, per_seconds, bucket=None):
    def decorator(f):
        name = bucket or f.__name__

        @wraps(f)
        def wrapper(*args, **kwargs):
            ident = (_wallet() or request.remote_addr or "anon").lower()
            key = f"{name}:{ident}"
            now = time.time()
            with _RL_LOCK:
                hits = _RL_HITS[key]
                while hits and hits[0] <= now - per_seconds:
                    hits.popleft()
                if len(hits) >= max_calls:
                    retry = max(1, int(per_seconds - (now - hits[0])))
                    return jsonify({
                        "success": False,
                        "error": "Rate limit exceeded. Please slow down.",
                        "retry_after": retry,
                    }), 429
                hits.append(now)
            return f(*args, **kwargs)
        return wrapper
    return decorator


def feature_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not feature_enabled():
            return jsonify({"success": False, "error": "P2P trading is currently disabled"}), 503
        return f(*args, **kwargs)
    return wrapper


def _wallet():
    return session.get("wallet") or session.get("wallet_address")


def _require_auth():
    wallet = _wallet()
    verified = session.get("verified") or session.get("ubi_verified")
    return wallet, verified


def auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        wallet, verified = _require_auth()
        if not wallet or not verified:
            return jsonify({"success": False, "error": "Authentication required"}), 401
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        wallet = _wallet()
        if not session.get("verified") or not wallet:
            return jsonify({"success": False, "error": "Authentication required"}), 401
        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({"success": False, "error": "Admin access required"}), 403
        return f(*args, **kwargs)
    return wrapper


def _same(a, b):
    return (a or "").strip().lower() == (b or "").strip().lower()


def _looks_like_tx_hash(value):
    value = (value or "").strip()
    return value.startswith("0x") and len(value) == 66 and all(c in "0123456789abcdefABCDEF" for c in value[2:])


def _db_error(e):
    """Pull a concise, readable message out of a Supabase/Postgrest error so the
    frontend can show *why* a write failed (e.g. a missing column or constraint)
    instead of a generic 500."""
    for attr in ("message",):
        val = getattr(e, attr, None)
        if val:
            return str(val)
    args = getattr(e, "args", None)
    if args and isinstance(args[0], dict):
        d = args[0]
        return d.get("message") or d.get("details") or str(d)
    return str(e)


@p2p_bp.errorhandler(Exception)
def _p2p_json_errors(e):
    """Always return JSON for /p2p/api/* so the frontend never tries to parse an
    HTML error page (the "Unexpected token '<'" symptom). Non-API routes keep
    their normal HTML error behaviour."""
    from werkzeug.exceptions import HTTPException

    if not request.path.startswith("/p2p/api"):
        raise e
    if isinstance(e, HTTPException):
        return jsonify({"success": False, "error": e.description}), e.code
    logger.exception("P2P API error on %s: %s", request.path, e)
    return jsonify({"success": False, "error": "Internal server error"}), 500


# ── Page ─────────────────────────────────────────────────────────────────────
@p2p_bp.route("/")
def p2p_home():
    if not feature_enabled():
        return redirect("/")
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return redirect("/login")
    wc_pid = os.environ.get("WALLETCONNECT_PROJECT_ID", "")
    has_explicit_sidecar = bool(os.getenv("WC_SERVICE_URL"))
    is_serverless_runtime = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
    wc_sidecar = has_explicit_sidecar or not is_serverless_runtime
    from supabase_client import is_admin
    return render_template(
        "p2p.html",
        wallet=wallet,
        p2p_contract=chain.P2P_ESCROW_CONTRACT_ADDRESS,
        gd_contract=GD_TOKEN_ADDRESS,
        chain_id=CHAIN_ID,
        min_lock_gd=chain.MIN_LOCK_GD,
        max_lock_gd=chain.MAX_LOCK_GD,
        walletconnect_project_id=wc_pid,
        walletconnect_sidecar_enabled=wc_sidecar,
        login_method=session.get("login_method", "walletconnect"),
        is_admin_user=is_admin(wallet),
    )


# ── Public reads ──────────────────────────────────────────────────────────────
@p2p_bp.route("/api/price-reference")
def api_price_reference():
    fiat = request.args.get("fiat", "")
    return jsonify({"success": True, "price": chain.get_gd_price_reference(fiat)})


def _sync_listing_from_chain(listing):
    """Attach live availability and close sold-out/inactive mirrored listings."""
    if not listing or not chain.is_configured():
        return listing
    oc = listing.get("onchain_id")
    if oc is None:
        return listing
    live = chain.get_listing(oc)
    if not live:
        return listing
    listing["available_gd"] = live["available_gd"]
    listing["onchain_active"] = live["active"]
    if listing.get("status") == "active" and (not live["active"] or float(live["available_gd"] or 0) <= 0):
        status = "closed" if float(live["available_gd"] or 0) <= 0 else "cancelled"
        updated = db.update_listing(listing["id"], {"status": status})
        if updated:
            updated["available_gd"] = live["available_gd"]
            updated["onchain_active"] = live["active"]
            return updated
        listing["status"] = status
    return listing


@p2p_bp.route("/api/listings")
def api_listings():
    seller = request.args.get("seller")
    include_inactive = request.args.get("include_inactive") in {"1", "true", "yes"}
    listings = db.list_listings_for_seller(seller) if seller and include_inactive else db.list_active_listings()
    # Merge live on-chain availability when the contract is configured, and
    # hide sold-out/inactive ads from the public Buy feed.
    listings = [_sync_listing_from_chain(l) for l in listings]
    if not include_inactive:
        listings = [l for l in listings if l.get("status") == "active" and float(l.get("available_gd", l.get("total_gd", 0)) or 0) > 0 and l.get("onchain_active", True)]
    return jsonify({"success": True, "listings": listings})


@p2p_bp.route("/api/payment-methods")
def api_payment_methods_public():
    seller = request.args.get("seller", "")
    if not seller:
        return jsonify({"success": False, "error": "Missing seller"}), 400
    return jsonify({"success": True, "payment_methods": db.list_payment_methods(seller)})


@p2p_bp.route("/api/quote")
def api_quote():
    listing_id = request.args.get("listing_id", "")
    amount_gd = request.args.get("amount_gd", "")
    try:
        amount = float(amount_gd)
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid amount_gd"}), 400
    listing = db.get_listing_row(listing_id)
    if not listing:
        return jsonify({"success": False, "error": "Listing not found"}), 404
    listing = _sync_listing_from_chain(listing)
    available_gd = float(listing.get("available_gd", listing.get("total_gd", 0)) or 0)
    if listing.get("status") != "active" or not listing.get("onchain_active", True) or available_gd <= 0:
        return jsonify({"success": False, "error": "This sell ad is no longer available."}), 409
    if amount > available_gd:
        return jsonify({
            "success": False,
            "error": f"Only {available_gd:g} G$ is still available for this ad. Refresh the listings and try a lower amount.",
            "available_gd": available_gd,
        }), 409
    price_usdt = float(listing["price_usdt"])
    usdt_total = amount * price_usdt
    quote = {
        "amount_gd": amount,
        "available_gd": available_gd,
        "price_usdt": price_usdt,
        "pay_amount_usdt": usdt_total,
        "pay_currency": "USDT",
    }
    fiat_currency = listing.get("fiat_currency")
    if fiat_currency:
        fiat_rate = listing.get("fiat_rate")
        if fiat_rate:
            quote["pay_amount_fiat"] = usdt_total * float(fiat_rate)
            quote["fiat_currency"] = fiat_currency
            quote["pay_currency"] = fiat_currency
        else:
            ref = chain.get_gd_price_reference(fiat_currency)
            if ref.get("fiat") and ref.get("usd"):
                implied = ref["fiat"] / ref["usd"]  # fiat per USD ~ per USDT
                quote["pay_amount_fiat"] = usdt_total * implied
                quote["fiat_currency"] = fiat_currency
                quote["pay_currency"] = fiat_currency
                quote["fiat_rate_source"] = "coingecko"
    quote["reference"] = chain.get_gd_price_reference(fiat_currency or "")
    return jsonify({"success": True, "quote": quote})


# ── Seller: payment methods + listings ────────────────────────────────────────
@p2p_bp.route("/api/payment-methods", methods=["POST"])
@feature_required
@rate_limit(20, 60)
@auth_required
def api_add_payment_method():
    wallet = _wallet()
    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "").strip()
    if not kind:
        return jsonify({"success": False, "error": "Missing kind"}), 400
    pm = db.add_payment_method(wallet, kind, data.get("label"), data.get("details") or {})
    return jsonify({"success": True, "payment_method": pm})


@p2p_bp.route("/api/listings", methods=["POST"])
@feature_required
@rate_limit(10, 60)
@auth_required
def api_create_listing():
    """Record an offchain listing AFTER the seller signed createListing on-chain."""
    wallet = _wallet()
    data = request.get_json(silent=True) or {}
    onchain_id = data.get("onchain_id")
    try:
        total_gd = float(data.get("total_gd"))
        min_order_gd = float(data.get("min_order_gd"))
        price_usdt = float(data.get("price_usdt"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid numeric fields"}), 400

    if total_gd < chain.MIN_LOCK_GD or total_gd > chain.MAX_LOCK_GD:
        return jsonify({"success": False, "error": "total_gd out of allowed range"}), 400
    if min_order_gd < chain.MIN_LOCK_GD or min_order_gd > total_gd:
        return jsonify({"success": False, "error": "invalid min_order_gd"}), 400
    if price_usdt <= 0:
        return jsonify({"success": False, "error": "price_usdt must be positive"}), 400

    # Verify the on-chain listing belongs to this seller.
    if chain.is_configured() and onchain_id is not None:
        live = chain.get_listing(onchain_id)
        if not live or not _same(live["seller"], wallet):
            return jsonify({"success": False, "error": "On-chain listing not found for this wallet"}), 400

    try:
        listing = db.create_listing(
            seller_wallet=wallet,
            total_gd=total_gd,
            min_order_gd=min_order_gd,
            price_usdt=price_usdt,
            fiat_currency=(data.get("fiat_currency") or None),
            fiat_rate=data.get("fiat_rate"),
            terms=data.get("terms"),
            onchain_id=onchain_id,
            create_tx_hash=data.get("create_tx_hash"),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("create_listing failed")
        return jsonify({"success": False, "error": f"Could not record listing: {_db_error(e)}"}), 500
    return jsonify({"success": True, "listing": listing})


@p2p_bp.route("/api/listings/<int:listing_id>/cancel", methods=["POST"])
@feature_required
@rate_limit(20, 60)
@auth_required
def api_cancel_listing(listing_id):
    wallet = _wallet()
    listing = db.get_listing_row(listing_id)
    if not listing:
        return jsonify({"success": False, "error": "Listing not found"}), 404
    if not _same(listing["seller_wallet"], wallet):
        return jsonify({"success": False, "error": "Not your listing"}), 403
    updated = db.update_listing(listing_id, {"status": "cancelled"})
    return jsonify({"success": True, "listing": updated})


# ── Buyer: orders ─────────────────────────────────────────────────────────────
@p2p_bp.route("/api/orders", methods=["POST"])
@feature_required
@rate_limit(15, 60)
@auth_required
def api_create_order():
    """Record an order AFTER the buyer signed openOrder on-chain."""
    wallet = _wallet()
    data = request.get_json(silent=True) or {}
    listing_id = data.get("listing_id")
    onchain_id = data.get("onchain_id")
    try:
        amount_gd = float(data.get("amount_gd"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "Invalid amount_gd"}), 400

    listing = db.get_listing_row(listing_id)
    if not listing:
        return jsonify({"success": False, "error": "Listing not found"}), 404
    if _same(listing["seller_wallet"], wallet):
        return jsonify({"success": False, "error": "Cannot buy your own listing"}), 400
    if amount_gd < float(listing["min_order_gd"]):
        return jsonify({"success": False, "error": "amount below seller minimum"}), 400

    if chain.is_configured() and onchain_id is not None:
        live = chain.get_order(onchain_id)
        if not live or not _same(live["buyer"], wallet):
            return jsonify({"success": False, "error": "On-chain order not found for this wallet"}), 400

    price_usdt = float(listing["price_usdt"])
    pay_amount = amount_gd * price_usdt
    pay_currency = "USDT"
    if listing.get("fiat_currency") and listing.get("fiat_rate"):
        pay_amount = pay_amount * float(listing["fiat_rate"])
        pay_currency = listing["fiat_currency"]

    window = get_env_int("P2P_PAYMENT_WINDOW_SECONDS", 1800)
    try:
        order = db.create_order(
            listing_row=listing,
            buyer_wallet=wallet,
            amount_gd=amount_gd,
            pay_amount=pay_amount,
            pay_currency=pay_currency,
            payment_method_id=data.get("payment_method_id"),
            onchain_id=onchain_id,
            open_tx_hash=data.get("open_tx_hash"),
            payment_window_seconds=window,
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("create_order failed")
        return jsonify({"success": False, "error": f"Could not record order: {_db_error(e)}"}), 500
    return jsonify({"success": True, "order": order})


def _order_party_guard(order, wallet, allow_admin=False):
    if _same(order["buyer_wallet"], wallet) or _same(order["seller_wallet"], wallet):
        return True
    if allow_admin:
        from supabase_client import is_admin
        return is_admin(wallet)
    return False


@p2p_bp.route("/api/orders/<int:order_id>")
@auth_required
def api_get_order(order_id):
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if not _order_party_guard(order, wallet, allow_admin=True):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    return jsonify({
        "success": True,
        "order": order,
        "listing": db.get_listing_row(order["listing_id"]),
        "proofs": db.list_proofs(order_id),
        "messages": db.list_messages(order_id),
    })


@p2p_bp.route("/api/orders")
@auth_required
def api_my_orders():
    wallet = _wallet()
    role = request.args.get("role", "buyer")
    return jsonify({"success": True, "orders": db.list_orders_for_wallet(wallet, role)})


@p2p_bp.route("/api/orders/<int:order_id>/mark-paid", methods=["POST"])
@feature_required
@rate_limit(20, 60)
@auth_required
def api_mark_paid(order_id):
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if not _same(order["buyer_wallet"], wallet):
        return jsonify({"success": False, "error": "Only the buyer can mark paid"}), 403
    if order.get("status") != "open":
        return jsonify({"success": False, "error": "This order is already marked paid or closed"}), 409
    data = request.get_json(silent=True) or {}
    updated = db.update_order(order_id, {"status": "paid", "paid_tx_hash": data.get("paid_tx_hash")})
    try:
        from notifications_service import notification_service
        notification_service.clear_user_cache(order.get("seller_wallet"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not clear seller notification cache for P2P order %s: %s", order_id, exc)
    return jsonify({"success": True, "order": updated})


@p2p_bp.route("/api/orders/<int:order_id>/proof", methods=["POST"])
@feature_required
@rate_limit(15, 60)
@auth_required
def api_upload_proof(order_id):
    """Buyer uploads a raw image; backend converts via the managed ImgBB key."""
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if not _same(order["buyer_wallet"], wallet):
        return jsonify({"success": False, "error": "Only the buyer can upload proof"}), 403
    if order.get("status") not in ("open", "paid"):
        return jsonify({"success": False, "error": "Proof upload is closed for this order"}), 409

    file = request.files.get("image")
    if not file:
        return jsonify({"success": False, "error": "Missing image file"}), 400
    from object_storage_client import upload_to_imgbb
    result = upload_to_imgbb(file)
    if not result.get("success"):
        return jsonify({"success": False, "error": result.get("error", "Upload failed")}), 502
    proof = db.add_proof(order_id, wallet, result["url"], request.form.get("reference"))
    return jsonify({"success": True, "proof": proof, "order_status": order.get("status")})


@p2p_bp.route("/api/orders/<int:order_id>/approve", methods=["POST"])
@feature_required
@rate_limit(20, 60)
@auth_required
def api_seller_approve(order_id):
    """Seller approved + signed releaseOrder on-chain; record the release."""
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if not _same(order["seller_wallet"], wallet):
        return jsonify({"success": False, "error": "Only the seller can approve"}), 403
    data = request.get_json(silent=True) or {}
    release_tx_hash = data.get("release_tx_hash")
    if not _looks_like_tx_hash(release_tx_hash):
        return jsonify({"success": False, "error": "Missing or invalid release transaction hash"}), 400
    updated = db.update_order(order_id, {"status": "released", "release_tx_hash": release_tx_hash})
    listing = db.get_listing_row(order.get("listing_id"))
    if listing:
        _sync_listing_from_chain(listing)
    return jsonify({"success": True, "order": updated})


@p2p_bp.route("/api/orders/<int:order_id>/reject", methods=["POST"])
@feature_required
@rate_limit(20, 60)
@auth_required
def api_seller_reject(order_id):
    """Seller rejects the proof — NO contract call. Escalate to admin review."""
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if not _same(order["seller_wallet"], wallet):
        return jsonify({"success": False, "error": "Only the seller can reject"}), 403
    data = request.get_json(silent=True) or {}
    reason = data.get("reason")
    updated = db.update_order(order_id, {"status": "seller_rejected", "reject_reason": reason})
    if not db.get_open_dispute_for_order(order_id):
        db.create_dispute(order_id, "seller_rejected", reason)
    return jsonify({"success": True, "order": updated})


@p2p_bp.route("/api/orders/<int:order_id>/cancel", methods=["POST"])
@feature_required
@rate_limit(20, 60)
@auth_required
def api_cancel_order(order_id):
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if not _order_party_guard(order, wallet):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    updated = db.update_order(order_id, {"status": "cancelled"})
    return jsonify({"success": True, "order": updated})


@p2p_bp.route("/api/orders/<int:order_id>/dispute", methods=["POST"])
@feature_required
@rate_limit(10, 60)
@auth_required
def api_raise_dispute(order_id):
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if not _order_party_guard(order, wallet):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    data = request.get_json(silent=True) or {}
    db.update_order(order_id, {"status": "disputed"})
    if not db.get_open_dispute_for_order(order_id):
        db.create_dispute(order_id, wallet, data.get("reason"))
    return jsonify({"success": True})


# ── Chat ───────────────────────────────────────────────────────────────────
@p2p_bp.route("/api/orders/<int:order_id>/messages", methods=["GET", "POST"])
@rate_limit(120, 60)
@auth_required
def api_messages(order_id):
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if not _order_party_guard(order, wallet, allow_admin=True):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    if request.method == "POST":
        data = request.get_json(silent=True) or {}
        body = (data.get("body") or "").strip()
        if not body:
            return jsonify({"success": False, "error": "Empty message"}), 400
        msg = db.add_message(order_id, wallet, body)
        return jsonify({"success": True, "message": msg})
    return jsonify({"success": True, "messages": db.list_messages(order_id)})


# ── Admin dashboard (owner-key review; SERVER-SIGNED with P2P_KEY) ────────────
@p2p_bp.route("/api/admin/review-queue")
@admin_required
def api_admin_review_queue():
    orders = db.list_orders_by_status(REVIEW_STATUSES)
    enriched = []
    for o in orders:
        enriched.append({
            "order": o,
            "listing": db.get_listing_row(o["listing_id"]),
            "proofs": db.list_proofs(o["id"]),
            "messages": db.list_messages(o["id"]),
            "dispute": db.get_open_dispute_for_order(o["id"]),
        })
    return jsonify({"success": True, "queue": enriched, "contract_configured": chain.is_configured()})


@p2p_bp.route("/api/admin/orders/<int:order_id>/release", methods=["POST"])
@rate_limit(30, 60)
@admin_required
def api_admin_release(order_id):
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if order.get("onchain_id") is None:
        return jsonify({"success": False, "error": "Order has no on-chain id"}), 400
    if not chain.P2P_KEY:
        return jsonify({"success": False, "error": "P2P_KEY not configured on server"}), 503
    try:
        result = chain.owner_release_order(order["onchain_id"])
    except Exception as exc:
        logger.exception("P2P admin release failed for order %s", order_id)
        return jsonify({
            "success": False,
            "error": f"On-chain release failed: {exc}",
            "explanation": "No database status was changed. Check the on-chain order/listing status and the server owner key before retrying.",
        }), 502
    if not result.get("success"):
        return jsonify({
            "success": False,
            "error": "On-chain release transaction reverted or was not confirmed",
            "tx_hash": result.get("tx_hash"),
            "explanation": "Funds remain in escrow unless the linked Celo transaction succeeded.",
        }), 502
    updated = db.update_order(order_id, {
        "status": "owner_released", "reviewed_by": wallet, "release_tx_hash": result["tx_hash"],
    })
    dispute = db.get_open_dispute_for_order(order_id)
    if dispute:
        db.resolve_dispute_row(dispute["id"], "resolved_buyer", wallet, result["tx_hash"])
    listing = db.get_listing_row(order.get("listing_id"))
    if listing:
        _sync_listing_from_chain(listing)
    return jsonify({"success": True, "order": updated, "tx_hash": result["tx_hash"]})


@p2p_bp.route("/api/admin/orders/<int:order_id>/refund", methods=["POST"])
@rate_limit(30, 60)
@admin_required
def api_admin_refund(order_id):
    wallet = _wallet()
    order = db.get_order_row(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    if order.get("onchain_id") is None:
        return jsonify({"success": False, "error": "Order has no on-chain id"}), 400
    if not chain.P2P_KEY:
        return jsonify({"success": False, "error": "P2P_KEY not configured on server"}), 503
    try:
        result = chain.owner_refund_order(order["onchain_id"])
    except Exception as exc:
        logger.exception("P2P admin refund failed for order %s", order_id)
        return jsonify({
            "success": False,
            "error": f"On-chain refund failed: {exc}",
            "explanation": "No database status was changed. Refund to seller means the reserved G$ returns to the listing's on-chain available balance, not a wallet transfer to the seller.",
        }), 502
    if not result.get("success"):
        return jsonify({
            "success": False,
            "error": "On-chain refund transaction reverted or was not confirmed",
            "tx_hash": result.get("tx_hash"),
            "explanation": "Funds remain in escrow unless the linked Celo transaction succeeded.",
        }), 502
    updated = db.update_order(order_id, {
        "status": "owner_refunded", "reviewed_by": wallet, "release_tx_hash": result["tx_hash"],
    })
    dispute = db.get_open_dispute_for_order(order_id)
    if dispute:
        db.resolve_dispute_row(dispute["id"], "resolved_seller", wallet, result["tx_hash"])
    listing = db.get_listing_row(order.get("listing_id"))
    synced_listing = _sync_listing_from_chain(listing) if listing else None
    return jsonify({
        "success": True,
        "order": updated,
        "listing": synced_listing,
        "tx_hash": result["tx_hash"],
        "message": "Refund completed: reserved G$ was returned to the seller listing's on-chain available balance.",
    })


@p2p_bp.route("/api/admin/worker-status")
@admin_required
def api_admin_worker_status():
    """Health/telemetry for the in-process auto-expiry + reconciliation worker."""
    from .expiry import get_expiry_scheduler
    return jsonify({
        "success": True,
        "feature_enabled": feature_enabled(),
        "contract_configured": chain.is_configured(),
        "worker": get_expiry_scheduler().get_status(),
    })
