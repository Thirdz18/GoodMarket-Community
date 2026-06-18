"""
Flask routes for the trustless P2P escrow flow.

Every route here either:
* returns an **unsigned** transaction payload that the user's wallet (the
  browser via WalletConnect / MiniPay) is expected to sign and broadcast,
  *or*
* returns read-only state combined from the on-chain contract and the
  Supabase mirror.

The only route that touches a private key on the server side is
``/p2p/admin/resolve-dispute``, which uses the ADMIN_KEY set on the
environment for arbiter actions.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import wraps
from typing import Any, Dict, Optional

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from .chat_service import (
    ATTACHMENT_MIME_TYPES as CHAT_ATTACHMENT_MIME_TYPES,
    ChatValidationError,
    MAX_ATTACHMENT_BYTES as CHAT_MAX_ATTACHMENT_BYTES,
    MAX_BODY_CHARS as CHAT_MAX_BODY_CHARS,
    chat_service,
)
from .escrow_service import escrow_service
from .indexer import get_indexer
from .proofs_service import (
    MAX_FILE_BYTES,
    MAX_PROOFS_PER_TRADE,
    ProofValidationError,
    guess_mime_type,
    proofs_service,
)

# Minimum delay between two messages from the same wallet in the same trade,
# enforced at the route layer to discourage burst-spam without needing a
# proper rate-limiter dependency.
CHAT_RATE_LIMIT_SECONDS = 1.0

logger = logging.getLogger(__name__)

p2p_bp = Blueprint("p2p", __name__)


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------


def _safe_limit(default: int = 50, cap: int = 200) -> int:
    """Parse the ``limit`` query arg without raising on garbage like ``?limit=abc``.

    Falls back to ``default`` for missing / non-numeric / non-positive values
    so we never bubble up a ValueError as an opaque HTTP 500.
    """
    raw = request.args.get("limit")
    if raw is None or raw == "":
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, cap)


def _wallet_from_session() -> str:
    return (session.get("wallet") or session.get("wallet_address") or "").lower()


def _is_admin(wallet: str) -> bool:
    """Return True if the connected wallet is the contract arbiter (ADMIN_KEY).

    Falls back to any address listed in the ``P2P_ADMIN_WALLETS`` env var
    (comma-separated) so we can support multiple admin reviewers without
    sharing the ADMIN_KEY.
    """
    import os

    if not wallet:
        return False
    wallet = wallet.lower()
    admin_addr = (escrow_service.contract.admin_address or "").lower()
    if wallet == admin_addr:
        return True
    extras = os.getenv("P2P_ADMIN_WALLETS", "")
    for addr in (a.strip().lower() for a in extras.split(",")):
        if addr and addr == wallet:
            return True
    return False


def p2p_auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("verified") or not _wallet_from_session():
            if request.is_json or request.path.startswith("/p2p/api/"):
                return jsonify(
                    {"success": False, "error": "Authentication required"}
                ), 401
            return redirect(url_for("home"))
        return f(*args, **kwargs)

    return wrapper


def p2p_terms_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("verified") or not _wallet_from_session():
            if request.is_json or request.path.startswith("/p2p/api/"):
                return jsonify(
                    {"success": False, "error": "Authentication required"}
                ), 401
            return redirect(url_for("home"))
        if not session.get("p2p_terms_accepted"):
            if request.is_json or request.path.startswith("/p2p/api/"):
                return jsonify(
                    {
                        "success": False,
                        "error": "P2P terms acceptance required",
                        "redirect": url_for("p2p.p2p_terms"),
                    }
                ), 403
            return redirect(url_for("p2p.p2p_terms"))
        return f(*args, **kwargs)

    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        wallet = _wallet_from_session()
        if not wallet or not session.get("verified"):
            return jsonify(
                {"success": False, "error": "Authentication required"}
            ), 401
        if not _is_admin(wallet):
            return jsonify({"success": False, "error": "Forbidden"}), 403
        return f(*args, **kwargs)

    return wrapper


def _json_body() -> Dict[str, Any]:
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------------------
# HTML pages
# ---------------------------------------------------------------------------


@p2p_bp.route("/terms")
@p2p_auth_required
def p2p_terms():
    return render_template("p2p_terms.html", wallet=_wallet_from_session())


@p2p_bp.route("/accept-terms", methods=["POST"])
@p2p_auth_required
def accept_p2p_terms():
    session["p2p_terms_accepted"] = True
    session.permanent = True
    return jsonify(
        {
            "success": True,
            "message": "P2P Trading terms accepted",
            "redirect_to": "/p2p/",
        }
    )


@p2p_bp.route("/")
@p2p_terms_required
def p2p_dashboard():
    wallet = _wallet_from_session()
    import os

    return render_template(
        "p2p_trading.html",
        wallet=wallet,
        contract=escrow_service.contract_status(),
        payment_methods=escrow_service.payment_methods,
        fiat_currencies=escrow_service.fiat_currencies,
        is_admin=_is_admin(wallet),
        login_method=session.get("login_method", "walletconnect"),
        walletconnect_project_id=os.environ.get("WALLETCONNECT_PROJECT_ID", ""),
        walletconnect_sidecar_enabled=_is_walletconnect_sidecar_enabled(),
    )


# ---------------------------------------------------------------------------
# Contract / config endpoints
# ---------------------------------------------------------------------------


@p2p_bp.route("/api/contract")
@p2p_auth_required
def api_contract_info():
    return jsonify({"success": True, **escrow_service.contract_status()})


@p2p_bp.route("/api/config")
@p2p_auth_required
def api_config():
    return jsonify(
        {
            "success": True,
            "payment_methods": escrow_service.payment_methods,
            "fiat_currencies": escrow_service.fiat_currencies,
            "min_ad_amount_gd": 20_000,
            "default_payment_window_seconds": (
                escrow_service.DEFAULT_PAYMENT_WINDOW_SECONDS
            ),
        }
    )


# ---------------------------------------------------------------------------
# Browse / read APIs
# ---------------------------------------------------------------------------


@p2p_bp.route("/api/ads")
@p2p_terms_required
def api_list_ads():
    wallet = _wallet_from_session()
    fiat = request.args.get("fiat_currency")
    method = request.args.get("payment_method")
    limit = _safe_limit()
    ads = escrow_service.list_open_ads(
        viewer_wallet=wallet,
        fiat_currency=fiat,
        payment_method=method,
        limit=limit,
    )
    return jsonify({"success": True, "ads": ads, "count": len(ads)})


@p2p_bp.route("/api/ads/mine")
@p2p_terms_required
def api_my_ads():
    wallet = _wallet_from_session()
    ads = escrow_service.get_my_ads(wallet)
    return jsonify({"success": True, "ads": ads, "count": len(ads)})


@p2p_bp.route("/api/trades/mine")
@p2p_terms_required
def api_my_trades():
    wallet = _wallet_from_session()
    limit = _safe_limit()
    trades = escrow_service.get_my_trades(wallet, limit=limit)
    return jsonify({"success": True, "trades": trades, "count": len(trades)})


@p2p_bp.route("/api/orders/<order_id>")
@p2p_terms_required
def api_get_order(order_id: str):
    order = escrow_service.get_order(order_id)
    if not order:
        return jsonify({"success": False, "error": "Order not found"}), 404
    return jsonify({"success": True, "order": order})


@p2p_bp.route("/api/trades/<trade_id>")
@p2p_terms_required
def api_get_trade(trade_id: str):
    trade = escrow_service.get_trade(trade_id)
    if not trade:
        return jsonify({"success": False, "error": "Trade not found"}), 404
    wallet = _wallet_from_session()
    if (
        wallet
        and wallet not in (
            (trade.get("buyer_wallet") or "").lower(),
            (trade.get("seller_wallet") or "").lower(),
        )
        and not _is_admin(wallet)
    ):
        return jsonify({"success": False, "error": "Forbidden"}), 403
    return jsonify({"success": True, "trade": trade})


# ---------------------------------------------------------------------------
# Tx-prep endpoints — return unsigned transactions for wallet signing
# ---------------------------------------------------------------------------


@p2p_bp.route("/api/ads/prepare-open", methods=["POST"])
@p2p_terms_required
def api_prepare_open_ad():
    wallet = _wallet_from_session()
    body = _json_body()
    try:
        result = escrow_service.prepare_open_ad(
            seller_wallet=wallet,
            total_g_dollar=float(body.get("total_g_dollar")),
            min_order_g_dollar=float(body.get("min_order_g_dollar")),
            max_order_g_dollar=float(body.get("max_order_g_dollar")),
            fiat_amount=float(body.get("fiat_amount")),
            fiat_currency=body.get("fiat_currency"),
            payment_method=body.get("payment_method"),
            payment_details=body.get("payment_details", ""),
            description=body.get("description", ""),
        )
    except (TypeError, ValueError) as exc:
        return jsonify({"success": False, "error": f"Invalid input: {exc}"}), 400
    return jsonify(result), (200 if result.get("success") else 400)


@p2p_bp.route("/api/ads/<order_id>/prepare-close", methods=["POST"])
@p2p_terms_required
def api_prepare_close_ad(order_id: str):
    wallet = _wallet_from_session()
    result = escrow_service.prepare_close_ad(wallet, order_id)
    return jsonify(result), (200 if result.get("success") else 400)


@p2p_bp.route("/api/orders/<order_id>/prepare-place", methods=["POST"])
@p2p_terms_required
def api_prepare_place_order(order_id: str):
    wallet = _wallet_from_session()
    body = _json_body()
    try:
        amount = float(body.get("amount_g_dollar"))
    except (TypeError, ValueError):
        return jsonify(
            {"success": False, "error": "Missing/invalid amount_g_dollar"}
        ), 400
    window = body.get("payment_window_seconds")
    try:
        window = int(window) if window is not None else None
    except (TypeError, ValueError):
        return jsonify(
            {"success": False, "error": "Invalid payment_window_seconds"}
        ), 400
    result = escrow_service.prepare_place_order(
        buyer_wallet=wallet,
        order_id=order_id,
        amount_g_dollar=amount,
        payment_window_seconds=window,
    )
    return jsonify(result), (200 if result.get("success") else 400)


@p2p_bp.route("/api/trades/<trade_id>/upload-proof", methods=["POST"])
@p2p_terms_required
def api_upload_proof(trade_id: str):
    wallet = _wallet_from_session()
    body = _json_body()
    proof_url = (body.get("proof_url") or "").strip()
    result = escrow_service.upload_payment_proof(wallet, trade_id, proof_url)
    return jsonify(result), (200 if result.get("success") else 400)


# ---------------------------------------------------------------------------
# Multi-file payment-proof attachments backed by Supabase Storage
# ---------------------------------------------------------------------------


def _trade_membership(wallet: str, trade_id: str) -> Dict[str, Any]:
    """Return ``{"trade": trade, "role": "buyer"|"seller"|"arbiter"}`` if the
    wallet is allowed to view/upload proofs for this trade, else
    ``{"error": ..., "status": int}``."""
    trade = escrow_service.get_trade(trade_id)
    if not trade:
        return {"error": "Trade not found", "status": 404}
    wallet_lower = (wallet or "").lower()
    buyer = (trade.get("buyer_wallet") or "").lower()
    seller = (trade.get("seller_wallet") or "").lower()
    if wallet_lower and wallet_lower == buyer:
        return {"trade": trade, "role": "buyer"}
    if wallet_lower and wallet_lower == seller:
        return {"trade": trade, "role": "seller"}
    if _is_admin(wallet_lower):
        return {"trade": trade, "role": "arbiter"}
    return {"error": "Forbidden", "status": 403}


@p2p_bp.route("/api/trades/<trade_id>/proofs", methods=["GET"])
@p2p_terms_required
def api_list_proofs(trade_id: str):
    wallet = _wallet_from_session()
    membership = _trade_membership(wallet, trade_id)
    if "error" in membership:
        return jsonify(
            {"success": False, "error": membership["error"]}
        ), membership["status"]

    proofs = proofs_service.list_for_trade(trade_id, with_signed_urls=True)
    safe = [
        {
            "id": p.get("id"),
            "trade_id": p.get("trade_id"),
            "uploader_wallet": p.get("uploader_wallet"),
            "mime_type": p.get("mime_type"),
            "size_bytes": p.get("size_bytes"),
            "original_name": p.get("original_name"),
            "created_at": p.get("created_at"),
            "view_url": url_for(
                "p2p.api_view_proof",
                trade_id=trade_id,
                proof_id=p.get("id"),
            ),
            "signed_url": p.get("signed_url"),
        }
        for p in proofs
    ]
    return jsonify({"success": True, "proofs": safe, "count": len(safe)})


@p2p_bp.route("/api/trades/<trade_id>/proof-upload", methods=["POST"])
@p2p_terms_required
def api_upload_proof_file(trade_id: str):
    """Accept a multipart file upload, store it in Supabase Storage, and
    record the metadata. Buyers / sellers / arbiters of the trade only.

    Form fields:
        file: required, the binary attachment.
    """
    wallet = _wallet_from_session()
    membership = _trade_membership(wallet, trade_id)
    if "error" in membership:
        return jsonify(
            {"success": False, "error": membership["error"]}
        ), membership["status"]

    upload = request.files.get("file")
    if upload is None:
        return jsonify(
            {"success": False, "error": "Missing 'file' field"}
        ), 400

    file_bytes = upload.read()
    if len(file_bytes) > MAX_FILE_BYTES:
        return jsonify(
            {
                "success": False,
                "error": f"File too large (max {MAX_FILE_BYTES} bytes)",
            }
        ), 413

    mime_type = (upload.mimetype or "").lower() or guess_mime_type(
        upload.filename or ""
    )

    try:
        row = proofs_service.upload(
            trade_id=trade_id,
            uploader_wallet=wallet,
            file_bytes=file_bytes,
            mime_type=mime_type,
            original_name=upload.filename,
        )
    except ProofValidationError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        logger.exception("proofs_service.upload failed")
        return jsonify({"success": False, "error": str(exc)}), 500

    # Mirror the latest proof's view URL into ``p2p_trades.payment_proof_url``
    # so the existing "Mark paid" gate (which checks payment_proof_url is
    # non-empty) keeps working without a DB schema change.
    if membership.get("role") == "buyer":
        view_url = url_for(
            "p2p.api_view_proof",
            trade_id=trade_id,
            proof_id=row.get("id"),
            _external=True,
        )
        try:
            escrow_service.upload_payment_proof(wallet, trade_id, view_url)
        except Exception:  # noqa: BLE001
            logger.exception(
                "Failed to mirror proof view_url to p2p_trades.payment_proof_url"
            )

    return jsonify(
        {
            "success": True,
            "proof": {
                "id": row.get("id"),
                "mime_type": row.get("mime_type"),
                "size_bytes": row.get("size_bytes"),
                "original_name": row.get("original_name"),
                "created_at": row.get("created_at"),
                "view_url": url_for(
                    "p2p.api_view_proof",
                    trade_id=trade_id,
                    proof_id=row.get("id"),
                ),
            },
        }
    )


@p2p_bp.route("/api/trades/<trade_id>/proofs/<proof_id>/view")
@p2p_terms_required
def api_view_proof(trade_id: str, proof_id: str):
    """Redirect the requesting buyer/seller/arbiter to a fresh signed URL
    for the stored proof. Re-validates membership on every request so an
    accidentally leaked URL cannot be replayed by an outsider."""
    wallet = _wallet_from_session()
    membership = _trade_membership(wallet, trade_id)
    if "error" in membership:
        return jsonify(
            {"success": False, "error": membership["error"]}
        ), membership["status"]

    proof = proofs_service.get_proof(proof_id)
    if not proof or proof.get("trade_id") != trade_id:
        return jsonify({"success": False, "error": "Proof not found"}), 404

    signed = proofs_service.signed_url(proof.get("storage_path"))
    if not signed:
        return jsonify(
            {"success": False, "error": "Failed to sign URL"}
        ), 500
    return redirect(signed, code=302)


@p2p_bp.route("/api/proofs/limits", methods=["GET"])
@p2p_terms_required
def api_proof_limits():
    return jsonify(
        {
            "success": True,
            "max_file_bytes": MAX_FILE_BYTES,
            "max_proofs_per_trade": MAX_PROOFS_PER_TRADE,
            "allowed_mime_types": [
                "image/png",
                "image/jpeg",
                "image/webp",
                "application/pdf",
            ],
        }
    )


# ---------------------------------------------------------------------------
# In-trade chat between buyer / seller / arbiter
# ---------------------------------------------------------------------------


def _chat_attachment_view_url(trade_id: str, message_id: str) -> str:
    return url_for(
        "p2p.api_chat_attachment_view",
        trade_id=trade_id,
        message_id=message_id,
    )


def _serialize_chat_message(
    msg: Dict[str, Any], trade_id: str
) -> Dict[str, Any]:
    """Shape a DB row for the API response. Never returns the raw storage
    path — clients always go through the signed-URL redirect endpoint."""
    out: Dict[str, Any] = {
        "id": msg.get("id"),
        "trade_id": msg.get("trade_id"),
        "sender_wallet": msg.get("sender_wallet"),
        "sender_role": msg.get("sender_role"),
        "body": msg.get("body"),
        "created_at": msg.get("created_at"),
    }
    if msg.get("attachment_path"):
        out["attachment"] = {
            "mime_type": msg.get("attachment_mime"),
            "size_bytes": msg.get("attachment_size"),
            "view_url": _chat_attachment_view_url(trade_id, msg.get("id")),
        }
    return out


@p2p_bp.route("/api/trades/<trade_id>/chat", methods=["GET"])
@p2p_terms_required
def api_list_chat(trade_id: str):
    """List chat messages for a trade. Buyer / seller / arbiter only.

    Optional ``since`` query arg (ISO-8601 timestamp) acts as a polling
    cursor — only messages strictly newer are returned.
    """
    wallet = _wallet_from_session()
    membership = _trade_membership(wallet, trade_id)
    if "error" in membership:
        return jsonify(
            {"success": False, "error": membership["error"]}
        ), membership["status"]

    since = (request.args.get("since") or "").strip() or None
    msgs = chat_service.list_for_trade(
        trade_id, since_iso=since, limit=_safe_limit(default=200, cap=500)
    )
    safe = [_serialize_chat_message(m, trade_id) for m in msgs]
    trade = membership["trade"]
    return jsonify(
        {
            "success": True,
            "messages": safe,
            "count": len(safe),
            "read_only": chat_service.is_read_only(trade),
            "your_role": membership["role"],
        }
    )


@p2p_bp.route("/api/trades/<trade_id>/chat", methods=["POST"])
@p2p_terms_required
def api_send_chat(trade_id: str):
    """Send a chat message (text and/or single image attachment).

    Accepts either ``application/json`` ``{"body": "..."}`` for text-only
    messages or ``multipart/form-data`` with ``body`` and/or ``file`` for
    attachments.
    """
    wallet = _wallet_from_session()
    membership = _trade_membership(wallet, trade_id)
    if "error" in membership:
        return jsonify(
            {"success": False, "error": membership["error"]}
        ), membership["status"]

    trade = membership["trade"]
    if chat_service.is_read_only(trade):
        return jsonify(
            {
                "success": False,
                "error": "Trade is closed; chat is read-only",
            }
        ), 409

    # Parse body + optional file from either JSON or multipart.
    body_text: Optional[str] = None
    file_bytes: Optional[bytes] = None
    file_mime: Optional[str] = None
    file_name: Optional[str] = None
    if request.content_type and request.content_type.startswith(
        "multipart/form-data"
    ):
        body_text = request.form.get("body")
        upload = request.files.get("file")
        if upload is not None:
            file_bytes = upload.read()
            if len(file_bytes) > CHAT_MAX_ATTACHMENT_BYTES:
                return jsonify(
                    {
                        "success": False,
                        "error": (
                            f"Attachment too large (max "
                            f"{CHAT_MAX_ATTACHMENT_BYTES} bytes)"
                        ),
                    }
                ), 413
            file_mime = (upload.mimetype or "").lower() or guess_mime_type(
                upload.filename or ""
            )
            file_name = upload.filename
    else:
        payload = _json_body()
        body_text = payload.get("body")

    # Lightweight rate limit: reject if the same sender already posted in
    # the last second. Protects against runaway scripts / accidental double-
    # clicks; not a substitute for a real abuse system.
    last = chat_service.latest_for_sender(trade_id, wallet)
    if last and last.get("created_at"):
        try:
            last_ts = datetime.fromisoformat(
                str(last["created_at"]).replace("Z", "+00:00")
            )
            now = datetime.now(timezone.utc)
            delta = (now - last_ts).total_seconds()
            if delta < CHAT_RATE_LIMIT_SECONDS:
                return jsonify(
                    {
                        "success": False,
                        "error": "Too many messages — please slow down",
                    }
                ), 429
        except Exception:  # noqa: BLE001
            # If timestamp parsing fails, fail open rather than block users.
            pass

    try:
        row = chat_service.send(
            trade_id=trade_id,
            sender_wallet=wallet,
            sender_role=membership["role"],
            body=body_text,
            file_bytes=file_bytes,
            mime_type=file_mime,
            original_name=file_name,
        )
    except ChatValidationError as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    except RuntimeError as exc:
        logger.exception("chat_service.send failed")
        return jsonify({"success": False, "error": str(exc)}), 500

    return jsonify(
        {
            "success": True,
            "message": _serialize_chat_message(row, trade_id),
        }
    )


@p2p_bp.route(
    "/api/trades/<trade_id>/chat/<message_id>/attachment", methods=["GET"]
)
@p2p_terms_required
def api_chat_attachment_view(trade_id: str, message_id: str):
    """Redirect to a fresh signed URL for a chat message's attachment."""
    wallet = _wallet_from_session()
    membership = _trade_membership(wallet, trade_id)
    if "error" in membership:
        return jsonify(
            {"success": False, "error": membership["error"]}
        ), membership["status"]

    msg = chat_service.get_message(message_id)
    if not msg or msg.get("trade_id") != trade_id:
        return jsonify({"success": False, "error": "Message not found"}), 404
    storage_path = msg.get("attachment_path")
    if not storage_path:
        return jsonify(
            {"success": False, "error": "No attachment on this message"}
        ), 404
    signed = chat_service.signed_url(storage_path)
    if not signed:
        return jsonify(
            {"success": False, "error": "Failed to sign URL"}
        ), 500
    return redirect(signed, code=302)


@p2p_bp.route("/api/chat/limits", methods=["GET"])
@p2p_terms_required
def api_chat_limits():
    return jsonify(
        {
            "success": True,
            "max_body_chars": CHAT_MAX_BODY_CHARS,
            "max_attachment_bytes": CHAT_MAX_ATTACHMENT_BYTES,
            "allowed_attachment_mime_types": sorted(CHAT_ATTACHMENT_MIME_TYPES),
            "rate_limit_seconds": CHAT_RATE_LIMIT_SECONDS,
        }
    )


@p2p_bp.route("/api/trades/<trade_id>/prepare-mark-paid", methods=["POST"])
@p2p_terms_required
def api_prepare_mark_paid(trade_id: str):
    wallet = _wallet_from_session()
    result = escrow_service.prepare_mark_paid(wallet, trade_id)
    return jsonify(result), (200 if result.get("success") else 400)


@p2p_bp.route("/api/trades/<trade_id>/prepare-release", methods=["POST"])
@p2p_terms_required
def api_prepare_release(trade_id: str):
    wallet = _wallet_from_session()
    result = escrow_service.prepare_release(wallet, trade_id)
    return jsonify(result), (200 if result.get("success") else 400)


@p2p_bp.route("/api/trades/<trade_id>/prepare-cancel", methods=["POST"])
@p2p_terms_required
def api_prepare_cancel(trade_id: str):
    wallet = _wallet_from_session()
    result = escrow_service.prepare_cancel_order(wallet, trade_id)
    return jsonify(result), (200 if result.get("success") else 400)


@p2p_bp.route("/api/trades/<trade_id>/prepare-dispute", methods=["POST"])
@p2p_terms_required
def api_prepare_dispute(trade_id: str):
    wallet = _wallet_from_session()
    result = escrow_service.prepare_dispute(wallet, trade_id)
    return jsonify(result), (200 if result.get("success") else 400)


@p2p_bp.route("/api/tx-submitted", methods=["POST"])
@p2p_terms_required
def api_tx_submitted():
    wallet = _wallet_from_session()
    body = _json_body()
    kind = body.get("kind")
    identifier = body.get("identifier")
    tx_hash = body.get("tx_hash")
    if kind not in ("ad", "trade") or not identifier or not tx_hash:
        return jsonify(
            {"success": False, "error": "kind, identifier, tx_hash required"}
        ), 400
    result = escrow_service.record_tx_submitted(
        kind, identifier, tx_hash, wallet
    )
    return jsonify(result), (200 if result.get("success") else 400)


# ---------------------------------------------------------------------------
# Admin / arbiter endpoints
# ---------------------------------------------------------------------------


@p2p_bp.route("/api/admin/disputes")
@admin_required
def api_admin_list_disputes():
    disputes = escrow_service.get_disputes()
    return jsonify({"success": True, "disputes": disputes})


@p2p_bp.route("/api/admin/disputes/<trade_id>/resolve", methods=["POST"])
@admin_required
def api_admin_resolve_dispute(trade_id: str):
    body = _json_body()
    if "buyer_wins" not in body or not isinstance(body["buyer_wins"], bool):
        return jsonify(
            {
                "success": False,
                "error": "buyer_wins (strict boolean) is required",
            }
        ), 400
    buyer_wins = body["buyer_wins"]
    arbiter = _wallet_from_session()
    result = escrow_service.resolve_dispute(trade_id, buyer_wins, arbiter)
    return jsonify(result), (200 if result.get("success") else 400)


# ---------------------------------------------------------------------------
# Indexer / health endpoints
# ---------------------------------------------------------------------------


@p2p_bp.route("/api/indexer/poll", methods=["POST"])
@admin_required
def api_indexer_poll():
    counts = get_indexer().poll_once()
    last = get_indexer().get_last_indexed_block()
    return jsonify(
        {"success": True, "events": counts, "last_indexed_block": last}
    )


@p2p_bp.route("/api/indexer/state")
@admin_required
def api_indexer_state():
    indexer = get_indexer()
    return jsonify(
        {
            "success": True,
            "last_indexed_block": indexer.get_last_indexed_block(),
            "head_block": indexer.w3.eth.block_number
            if indexer.w3.is_connected()
            else None,
            "contract_address": indexer.contract.address,
            "deployed_block": indexer.contract.deployed_block,
        }
    )


# ---------------------------------------------------------------------------
# Module init helper, called from main.py
# ---------------------------------------------------------------------------


def init_p2p_trading(app) -> None:
    """Register the blueprint and (optionally) start the background indexer.

    The indexer is opt-in via the ``P2P_INDEXER_ENABLED`` env var so unit
    tests and short-lived workers don't spin up background threads.
    """
    import os

    app.register_blueprint(p2p_bp, url_prefix="/p2p")
    if os.getenv("P2P_INDEXER_ENABLED", "").lower() in ("1", "true", "yes"):
        try:
            get_indexer().start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("Failed to start P2P escrow indexer: %s", exc)
