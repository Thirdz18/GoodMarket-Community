import logging
import uuid
from datetime import datetime
from flask import Blueprint, request, jsonify, render_template, session, redirect

from .client import reloadly_client
from .service import (
    usd_to_gd, get_gd_usd_price,
    auto_detect_gd_payment, verify_gd_payment, refund_gd,
    create_order_record, update_order_record,
    get_order_record, get_user_orders, sanitize_error
)

logger = logging.getLogger(__name__)

reloadly_bp = Blueprint("reloadly", __name__, url_prefix="/reloadly")


def _require_auth():
    wallet = session.get("wallet") or session.get("wallet_address")
    verified = session.get("verified") or session.get("ubi_verified")
    return wallet, verified


# ─── PAGES ─────────────────────────────────────────────────────────────────────

@reloadly_bp.route("/")
def reloadly_home():
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return redirect("/")
    import os
    gd_price = get_gd_usd_price()
    has_explicit_sidecar = bool(os.getenv("WC_SERVICE_URL"))
    is_serverless_runtime = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
    wc_sidecar = has_explicit_sidecar or not is_serverless_runtime
    return render_template(
        "reloadly.html",
        wallet=wallet,
        gd_price=gd_price,
        is_sandbox=reloadly_client.is_sandbox,
        merchant_address=os.getenv("MERCHANT_ADDRESS", ""),
        gd_contract=os.getenv("GOODDOLLAR_CONTRACT", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"),
        walletconnect_project_id=os.getenv("WALLETCONNECT_PROJECT_ID", ""),
        walletconnect_sidecar_enabled=wc_sidecar,
        login_method=session.get("login_method", "walletconnect"),
    )


@reloadly_bp.route("/api/countries")
def api_countries():
    wallet, verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401
    try:
        countries = reloadly_client.get_countries()
        return jsonify({"success": True, "countries": countries})
    except Exception as e:
        return jsonify({"success": False, "error": sanitize_error(e)}), 500


@reloadly_bp.route("/api/gd-price")
def api_gd_price():
    """Return current live G$ price from CoinGecko (no auth required)."""
    price = get_gd_usd_price()
    return jsonify({"success": True, "gd_usd_price": price, "gd_per_usd": round(1 / price) if price else 0})


# ─── TOP-UP ─────────────────────────────────────────────────────────────────────

@reloadly_bp.route("/api/topup/operators")
def api_topup_operators():
    wallet, verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401
    country = request.args.get("country", "PH")
    try:
        operators = reloadly_client.get_topup_operators(country)
        gd_price = get_gd_usd_price()
        for op in operators:
            # fixedAmounts are in USD (senderCurrencyCode) — convert directly to G$
            fixed_amounts = op.get("fixedAmounts", [])  # USD
            if fixed_amounts:
                op["gd_amounts"] = [round(a / gd_price) for a in fixed_amounts]
            # minAmount / maxAmount are also in USD
            min_amt = op.get("minAmount")
            max_amt = op.get("maxAmount")
            if min_amt is not None:
                op["gd_min"] = round(min_amt / gd_price)
            if max_amt is not None:
                op["gd_max"] = round(max_amt / gd_price)
        return jsonify({"success": True, "operators": operators, "gd_price": gd_price})
    except Exception as e:
        logger.error(f"api_topup_operators error: {e}")
        return jsonify({"success": False, "error": sanitize_error(e)}), 500


@reloadly_bp.route("/api/topup/detect-operator")
def api_detect_operator():
    wallet, verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401
    phone = request.args.get("phone", "")
    country = request.args.get("country", "PH")
    if not phone:
        return jsonify({"success": False, "error": "phone required"}), 400
    try:
        result = reloadly_client.auto_detect_operator(phone, country)
        return jsonify({"success": True, "operator": result})
    except Exception as e:
        return jsonify({"success": False, "error": sanitize_error(e)}), 500


# ─── GIFT CARDS ─────────────────────────────────────────────────────────────────

# Keywords used to identify virtual prepaid money-card products
# (Visa / Mastercard / American Express virtual/prepaid cards) across
# Reloadly's product catalog. Matched case-insensitively against product
# name and category name.
VIRTUAL_CARD_KEYWORDS = (
    "visa",
    "mastercard",
    "master card",
    "amex",
    "american express",
    "money card",
    "prepaid card",
)


def _is_virtual_card_product(product: dict) -> bool:
    """Return True if a Reloadly gift-card product is a virtual prepaid money card."""
    name = (product.get("productName") or "").lower()
    category = ""
    cat_obj = product.get("category")
    if isinstance(cat_obj, dict):
        category = (cat_obj.get("name") or "").lower()
    elif isinstance(cat_obj, str):
        category = cat_obj.lower()
    brand = ""
    brand_obj = product.get("brand")
    if isinstance(brand_obj, dict):
        brand = (brand_obj.get("brandName") or "").lower()
    haystack = f"{name} {category} {brand}"
    return any(kw in haystack for kw in VIRTUAL_CARD_KEYWORDS)


@reloadly_bp.route("/api/virtual-cards")
def api_virtual_cards():
    """
    Return Reloadly gift-card products that are virtual prepaid money cards
    (Visa / Mastercard / Amex). These can be funded with G$ and produce a
    card number + CVV/PIN + expiry that the user can spend at any online
    merchant that accepts the underlying network.
    """
    wallet, _verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401
    country = request.args.get("country", None)
    try:
        # Walk all pages (up to a safe cap) so we capture virtual cards that
        # may not appear on the first page of the broader gift-card catalog.
        matched: list = []
        seen_ids: set = set()
        page = 1
        MAX_PAGES = 10  # safety cap
        while page <= MAX_PAGES:
            data = reloadly_client.get_giftcard_products(
                country_code=country, page=page, size=50
            )
            products = data.get("content", data) if isinstance(data, dict) else data
            if not products:
                break
            for p in products:
                pid = p.get("productId") or p.get("id")
                if pid in seen_ids:
                    continue
                seen_ids.add(pid)
                if _is_virtual_card_product(p):
                    matched.append(p)
            # Stop early if Reloadly paging metadata says we're done.
            if isinstance(data, dict):
                total_pages = data.get("totalPages")
                if total_pages is not None and page >= int(total_pages):
                    break
            page += 1

        gd_price = get_gd_usd_price()
        for p in matched:
            fixed = p.get("fixedRecipientDenominations", [])
            if fixed:
                p["gd_fixed"] = [round(d / gd_price, 0) for d in fixed]
            min_r = p.get("minRecipientDenomination")
            if min_r:
                p["gd_min"] = round(min_r / gd_price, 0)
            max_r = p.get("maxRecipientDenomination")
            if max_r:
                p["gd_max"] = round(max_r / gd_price, 0)

        return jsonify({
            "success": True,
            "products": matched,
            "gd_price": gd_price,
            "count": len(matched),
        })
    except Exception as e:
        logger.error(f"api_virtual_cards error: {e}")
        return jsonify({"success": False, "error": sanitize_error(e)}), 500


@reloadly_bp.route("/api/giftcards")
def api_giftcards():
    wallet, verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401
    country = request.args.get("country", None)
    page = int(request.args.get("page", 1))
    size = int(request.args.get("size", 20))
    try:
        data = reloadly_client.get_giftcard_products(country_code=country, page=page, size=size)
        gd_price = get_gd_usd_price()
        products = data.get("content", data) if isinstance(data, dict) else data
        for p in products:
            min_d = p.get("minRecipientDenomination") or p.get("senderCurrencyCode") and None
            fixed = p.get("fixedRecipientDenominations", [])
            if fixed:
                p["gd_fixed"] = [round(d / gd_price, 0) for d in fixed]
            min_r = p.get("minRecipientDenomination")
            if min_r:
                p["gd_min"] = round(min_r / gd_price, 0)
            max_r = p.get("maxRecipientDenomination")
            if max_r:
                p["gd_max"] = round(max_r / gd_price, 0)
        return jsonify({"success": True, "products": products, "gd_price": gd_price})
    except Exception as e:
        logger.error(f"api_giftcards error: {e}")
        return jsonify({"success": False, "error": sanitize_error(e)}), 500


# ─── UTILITY ────────────────────────────────────────────────────────────────────

@reloadly_bp.route("/api/utility/billers")
def api_utility_billers():
    wallet, verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401
    country = request.args.get("country", None)
    try:
        billers = reloadly_client.get_utility_billers(country_code=country)
        return jsonify({"success": True, "billers": billers})
    except Exception as e:
        logger.error(f"api_utility_billers error: {e}")
        return jsonify({"success": False, "error": sanitize_error(e)}), 500


# ─── ORDERS ─────────────────────────────────────────────────────────────────────

@reloadly_bp.route("/api/order/prepare", methods=["POST"])
def api_prepare_order():
    """
    Step 1: Create a pending order record and return the G$ amount to pay.
    Frontend then triggers WalletConnect payment to MERCHANT_ADDRESS.
    """
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.json or {}
    order_type = data.get("order_type")
    usd_amount = data.get("usd_amount")

    if not order_type or not usd_amount:
        return jsonify({"success": False, "error": "order_type and usd_amount required"}), 400

    if order_type not in ("topup", "giftcard", "utility"):
        return jsonify({"success": False, "error": "Invalid order_type"}), 400

    try:
        gd_amount = usd_to_gd(float(usd_amount))
        order_id = str(uuid.uuid4())

        order_data = {
            "id": order_id,
            "wallet_address": wallet.lower(),
            "order_type": order_type,
            "status": "pending_payment",
            "usd_amount": float(usd_amount),
            "gd_amount": gd_amount,
            "gd_usd_price": get_gd_usd_price(),
            "order_payload": data.get("payload", {}),
            "created_at": datetime.utcnow().isoformat()
        }

        result = create_order_record(order_data)
        if not result["success"]:
            return jsonify({"success": False, "error": result["error"]}), 500

        return jsonify({
            "success": True,
            "order_id": order_id,
            "gd_amount": gd_amount,
            "usd_amount": float(usd_amount),
            "gd_usd_price": get_gd_usd_price()
        })

    except Exception as e:
        logger.error(f"api_prepare_order error: {e}")
        return jsonify({"success": False, "error": sanitize_error(e)}), 500


@reloadly_bp.route("/api/order/confirm", methods=["POST"])
def api_confirm_order():
    """
    Step 2: User has paid G$ — provide tx_hash to verify and process.
    Verifies blockchain payment → calls Reloadly → refunds on failure.
    """
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.json or {}
    order_id = data.get("order_id")
    tx_hash = data.get("tx_hash")

    if not order_id or not tx_hash:
        return jsonify({"success": False, "error": "order_id and tx_hash required"}), 400

    order_res = get_order_record(order_id)
    if not order_res["success"]:
        return jsonify({"success": False, "error": "Order not found"}), 404

    order = order_res["order"]

    if order["wallet_address"].lower() != wallet.lower():
        return jsonify({"success": False, "error": "Order does not belong to you"}), 403

    if order["status"] != "pending_payment":
        return jsonify({"success": False, "error": f"Order status is '{order['status']}', cannot confirm"}), 400

    # Mark as verifying
    update_order_record(order_id, {"status": "verifying", "tx_hash": tx_hash})

    # Verify blockchain payment
    verify = verify_gd_payment(wallet, order["gd_amount"], tx_hash)
    if not verify["success"]:
        update_order_record(order_id, {
            "status": "payment_failed",
            "failure_reason": verify.get("error", "Payment verification failed")
        })
        return jsonify({"success": False, "error": verify.get("error", "Payment verification failed")}), 400

    # Payment verified — process with Reloadly
    update_order_record(order_id, {"status": "processing"})

    try:
        payload = order.get("order_payload", {})
        order_type = order["order_type"]
        reloadly_result = None

        if order_type == "topup":
            reloadly_result = reloadly_client.send_topup(
                operator_id=payload["operator_id"],
                amount=payload["amount"],
                phone_number=payload["phone"],
                country_code=payload["country"],
                custom_identifier=order_id
            )
        elif order_type == "giftcard":
            reloadly_result = reloadly_client.order_giftcard(
                product_id=payload["product_id"],
                quantity=payload.get("quantity", 1),
                unit_price=payload["unit_price"],
                custom_identifier=order_id
            )
        elif order_type == "utility":
            reloadly_result = reloadly_client.pay_utility(
                biller_id=payload["biller_id"],
                amount=payload["amount"],
                subscriber_id=payload["subscriber_id"],
                custom_identifier=order_id
            )

        if reloadly_result:
            update_order_record(order_id, {
                "status": "completed",
                "reloadly_transaction_id": str(reloadly_result.get("transactionId") or reloadly_result.get("id") or ""),
                "reloadly_response": reloadly_result,
                "completed_at": datetime.utcnow().isoformat()
            })
            return jsonify({
                "success": True,
                "order_id": order_id,
                "status": "completed",
                "result": reloadly_result
            })
        else:
            raise Exception("No response from Reloadly")

    except Exception as e:
        logger.error(f"❌ Reloadly fulfillment failed for order {order_id}: {e}")

        # Attempt refund
        refund_result = refund_gd(wallet, order["gd_amount"], order_id)
        refund_status = "refunded" if refund_result["success"] else "refund_failed"
        update_order_record(order_id, {
            "status": refund_status,
            "failure_reason": sanitize_error(e),
            "refund_tx_hash": refund_result.get("tx_hash"),
            "refund_error": refund_result.get("error") if not refund_result["success"] else None
        })

        msg = f"Reloadly fulfillment failed. "
        if refund_result["success"]:
            msg += f"Your {order['gd_amount']} G$ has been refunded. Tx: {refund_result['tx_hash']}"
        else:
            msg += f"Refund also failed — please contact support. Order: {order_id}"

        return jsonify({
            "success": False,
            "error": msg,
            "order_id": order_id,
            "refunded": refund_result["success"],
            "refund_tx": refund_result.get("tx_hash")
        }), 500


@reloadly_bp.route("/api/order/detect-payment", methods=["POST"])
def api_detect_payment():
    """
    Auto-detect: scan recent Celo blocks for a G$ transfer matching the order.
    Frontend calls this repeatedly (poll) after initiating wallet payment.
    Returns {found: true, tx_hash} when detected, then processes the order.
    """
    wallet, verified = _require_auth()
    if not wallet or not verified:
        return jsonify({"error": "Not authenticated"}), 401

    data = request.json or {}
    order_id = data.get("order_id")
    if not order_id:
        return jsonify({"success": False, "error": "order_id required"}), 400

    order_res = get_order_record(order_id)
    if not order_res["success"]:
        return jsonify({"success": False, "error": "Order not found"}), 404

    order = order_res["order"]
    if order["wallet_address"].lower() != wallet.lower():
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    # If already processed, return current status
    if order["status"] not in ("pending_payment", "verifying"):
        return jsonify({
            "success": True,
            "found": True,
            "already_processed": True,
            "status": order["status"],
            "order_id": order_id
        })

    # Mark as verifying to prevent duplicate processing
    if order["status"] == "pending_payment":
        update_order_record(order_id, {"status": "verifying"})

    # If a specific tx_hash was provided, try verifying it directly first
    provided_tx = data.get("tx_hash")
    if provided_tx:
        verify = verify_gd_payment(wallet, float(order["gd_amount"]), provided_tx)
        if verify.get("success"):
            detect = {"verified": True, "tx_hash": provided_tx, "amount_gd": verify.get("amount_gd")}
        else:
            err = verify.get("error", "")
            if verify.get("reverted"):
                # Definitively failed on-chain — stop immediately, no refund needed
                update_order_record(order_id, {
                    "status": "payment_failed",
                    "tx_hash": provided_tx,
                    "failure_reason": err
                })
                return jsonify({
                    "success": False,
                    "found": True,
                    "status": "payment_failed",
                    "error": "❌ " + err + " — No G$ was deducted."
                }), 400
            # Still pending or not found — fall through to auto-detect
            detect = auto_detect_gd_payment(wallet, float(order["gd_amount"]))
    else:
        # Scan blockchain for matching G$ transfer
        detect = auto_detect_gd_payment(wallet, float(order["gd_amount"]))

    if not detect.get("verified"):
        # Not found yet — reset to pending_payment so frontend can poll again
        update_order_record(order_id, {"status": "pending_payment"})
        return jsonify({
            "success": True,
            "found": False,
            "message": detect.get("error", "Payment not detected yet — still watching blockchain...")
        })

    # Payment found! Save tx_hash and process
    tx_hash = detect["tx_hash"]
    update_order_record(order_id, {"tx_hash": tx_hash, "status": "processing"})

    try:
        payload = order.get("order_payload", {})
        order_type = order["order_type"]
        reloadly_result = None

        if order_type == "topup":
            reloadly_result = reloadly_client.send_topup(
                operator_id=payload["operator_id"],
                amount=payload["amount"],
                phone_number=payload["phone"],
                country_code=payload["country"],
                custom_identifier=order_id
            )
        elif order_type == "giftcard":
            reloadly_result = reloadly_client.order_giftcard(
                product_id=payload["product_id"],
                quantity=payload.get("quantity", 1),
                unit_price=payload["unit_price"],
                custom_identifier=order_id
            )
        elif order_type == "utility":
            reloadly_result = reloadly_client.pay_utility(
                biller_id=payload["biller_id"],
                amount=payload["amount"],
                subscriber_id=payload["subscriber_id"],
                custom_identifier=order_id
            )

        if reloadly_result:
            update_order_record(order_id, {
                "status": "completed",
                "reloadly_transaction_id": str(reloadly_result.get("transactionId") or reloadly_result.get("id") or ""),
                "reloadly_response": reloadly_result,
                "completed_at": datetime.utcnow().isoformat()
            })
            return jsonify({
                "success": True,
                "found": True,
                "status": "completed",
                "tx_hash": tx_hash,
                "order_id": order_id
            })
        else:
            raise Exception("No response from Reloadly")

    except Exception as e:
        logger.error(f"❌ Auto-detect Reloadly fulfillment failed for {order_id}: {e}")
        refund_result = refund_gd(wallet, order["gd_amount"], order_id)
        refund_status = "refunded" if refund_result["success"] else "refund_failed"
        update_order_record(order_id, {
            "status": refund_status,
            "failure_reason": sanitize_error(e),
            "refund_tx_hash": refund_result.get("tx_hash"),
            "refund_error": refund_result.get("error") if not refund_result["success"] else None
        })
        msg = f"Fulfillment failed. "
        if refund_result["success"]:
            msg += f"Your {order['gd_amount']} G$ has been refunded."
        else:
            msg += f"Refund failed — contact support. Order: {order_id}"
        return jsonify({
            "success": False,
            "found": True,
            "tx_hash": tx_hash,
            "error": msg,
            "refunded": refund_result["success"],
            "refund_tx": refund_result.get("tx_hash")
        }), 500


@reloadly_bp.route("/api/order/<order_id>")
def api_get_order(order_id):
    wallet, verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401
    res = get_order_record(order_id)
    if not res["success"]:
        return jsonify({"success": False, "error": "Order not found"}), 404
    order = res["order"]
    if order["wallet_address"].lower() != wallet.lower():
        return jsonify({"error": "Unauthorized"}), 403
    return jsonify({"success": True, "order": order})


@reloadly_bp.route("/api/order/<order_id>/card-details")
def api_get_card_details(order_id):
    """
    Return the virtual card details (card number, CVV/PIN, expiry) for a
    completed gift-card order owned by the authenticated wallet.

    Only available after the order is 'completed' and only to the wallet
    that placed the order. Card details are fetched live from Reloadly and
    NOT persisted — this endpoint acts as a thin proxy.
    """
    wallet, _verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401

    res = get_order_record(order_id)
    if not res["success"]:
        return jsonify({"success": False, "error": "Order not found"}), 404

    order = res["order"]
    if order["wallet_address"].lower() != wallet.lower():
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    if order.get("order_type") != "giftcard":
        return jsonify({"success": False, "error": "Order is not a gift/virtual card"}), 400

    if order.get("status") != "completed":
        return jsonify({
            "success": False,
            "error": f"Order status is '{order.get('status')}', card details not yet available",
        }), 400

    tx_id = order.get("reloadly_transaction_id")
    if not tx_id:
        return jsonify({"success": False, "error": "Missing Reloadly transaction id"}), 400

    try:
        cards = reloadly_client.get_giftcard_redeem_code(tx_id)
        return jsonify({"success": True, "cards": cards, "order_id": order_id})
    except Exception as e:
        logger.error(f"api_get_card_details error for {order_id}: {e}")
        return jsonify({"success": False, "error": sanitize_error(e)}), 500


@reloadly_bp.route("/api/orders")
def api_my_orders():
    wallet, verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401
    limit = int(request.args.get("limit", 20))
    orders = get_user_orders(wallet, limit=limit)
    return jsonify({"success": True, "orders": orders})


@reloadly_bp.route("/api/order/<order_id>/cancel", methods=["POST"])
def api_cancel_order(order_id):
    """Mark a pending order as expired or cancelled (only if no payment was made)."""
    wallet, verified = _require_auth()
    if not wallet:
        return jsonify({"error": "Not authenticated"}), 401

    order_res = get_order_record(order_id)
    if not order_res["success"]:
        return jsonify({"success": False, "error": "Order not found"}), 404

    order = order_res["order"]
    if order["wallet_address"].lower() != wallet.lower():
        return jsonify({"success": False, "error": "Unauthorized"}), 403

    # Only cancel if no payment has been detected/processed
    cancellable = {"pending_payment", "verifying"}
    if order["status"] not in cancellable:
        return jsonify({"success": False, "error": f"Cannot cancel order with status '{order['status']}'"}), 400

    data = request.json or {}
    new_status = data.get("status", "cancelled")
    if new_status not in ("cancelled", "expired"):
        new_status = "cancelled"

    update_order_record(order_id, {"status": new_status})
    return jsonify({"success": True, "status": new_status})
