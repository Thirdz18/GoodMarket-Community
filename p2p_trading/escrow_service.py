"""
P2P escrow orchestration on top of the on-chain ``GoodMarketP2PEscrow``.

This module is the layer the Flask routes call into. It owns:

* mapping between *off-chain* ad/trade rows in Supabase (which carry the
  human-readable payment method, currency, fiat amount, etc.) and the
  *on-chain* Ad/Trade structs (which only carry G$ amounts and ids);
* preparing unsigned transactions for the user's wallet to sign for every
  state transition (open ad, place order, cancel, mark paid, release,
  dispute);
* recording transaction hashes that the frontend submits, so the indexer
  can resolve the row even before the indexer-side scan reaches that block;
* providing read APIs that combine on-chain truth with off-chain context
  for the UI (listings, order history, trade status, dispute view).

Design notes:

* The DB row is always created **first**, before the user signs. We mint a
  random ``ad_id_onchain`` / ``trade_id_onchain`` server-side and embed it
  in the prepared transaction so the indexer can resolve the resulting
  event back to the row. Until the user actually broadcasts their signed
  transaction, the row stays in ``onchain_status='pending_user_signature'``.
* The status field has two layers: ``status`` is the off-chain workflow
  state (e.g. ``draft``, ``proof_uploaded``); ``onchain_status`` mirrors
  the contract's TradeStatus / Ad open|closed.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Union

from .contract import (
    P2PEscrowContract,
    TRADE_STATUS_NAMES,
    _from_wei,
    _to_wei,
    get_contract,
    make_random_bytes32,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Custom error classes for better error categorization
# ---------------------------------------------------------------------------

class EscrowServiceError(Exception):
    """Base exception for escrow service errors."""
    def __init__(self, message: str, error_code: str = "UNKNOWN_ERROR", details: Optional[Dict] = None):
        super().__init__(message)
        self.message = message
        self.error_code = error_code
        self.details = details or {}


class BlockchainError(EscrowServiceError):
    """Raised when blockchain RPC or contract calls fail."""
    def __init__(self, message: str, details: Optional[Dict] = None):
        super().__init__(message, "BLOCKCHAIN_ERROR", details)


class DatabaseError(EscrowServiceError):
    """Raised when Supabase database operations fail."""
    def __init__(self, message: str, details: Optional[Dict] = None):
        super().__init__(message, "DATABASE_ERROR", details)


class ValidationError(EscrowServiceError):
    """Raised when input validation fails."""
    def __init__(self, message: str, details: Optional[Dict] = None):
        super().__init__(message, "VALIDATION_ERROR", details)


class StateError(EscrowServiceError):
    """Raised when an operation is invalid for the current trade/ad state."""
    def __init__(self, message: str, details: Optional[Dict] = None):
        super().__init__(message, "STATE_ERROR", details)


# Helper to categorize and format errors for API responses
def _format_error_response(exc: Exception) -> Dict[str, Any]:
    """Format an exception into a standardized API error response."""
    if isinstance(exc, EscrowServiceError):
        return {
            "success": False,
            "error": exc.message,
            "error_code": exc.error_code,
            "details": exc.details,
        }
    
    # Categorize unknown exceptions
    error_str = str(exc).lower()
    if any(keyword in error_str for keyword in ["rpc", "connection", "timeout", "network", "web3"]):
        return {
            "success": False,
            "error": "Blockchain network temporarily unavailable. Please try again.",
            "error_code": "BLOCKCHAIN_ERROR",
            "suggestion": "Wait a moment and retry, or check your network connection.",
        }
    elif any(keyword in error_str for keyword in ["supabase", "database", "db", "connection pool"]):
        return {
            "success": False,
            "error": "Database temporarily unavailable. Please try again.",
            "error_code": "DATABASE_ERROR",
            "suggestion": "If the problem persists, contact support.",
        }
    else:
        logger.exception("Unexpected error in escrow_service")
        return {
            "success": False,
            "error": "An unexpected error occurred. Please try again.",
            "error_code": "UNKNOWN_ERROR",
        }


# Supported off-chain context. The contract doesn't care about these — they
# show up in the UI and DB only.
SUPPORTED_PAYMENT_METHODS = [
    "GCash", "PayMaya", "BPI", "BDO", "UnionBank", "Metrobank",
    "PayPal", "Wise", "Remitly", "Western Union",
    "USDC", "USDT", "Binance Pay", "Coins.ph", "Other",
]
SUPPORTED_FIAT_CURRENCIES = ["PHP", "USD", "EUR", "GBP", "CAD", "AUD", "SGD"]


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hex(b: bytes) -> str:
    return "0x" + b.hex()


_TX_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


class P2PEscrowService:
    """Orchestration of the on-chain P2P escrow + off-chain DB rows."""

    DEFAULT_PAYMENT_WINDOW_SECONDS = (
        P2PEscrowContract.DEFAULT_PAYMENT_WINDOW_SECONDS
    )

    payment_methods = SUPPORTED_PAYMENT_METHODS
    fiat_currencies = SUPPORTED_FIAT_CURRENCIES

    def __init__(
        self,
        contract: Optional[P2PEscrowContract] = None,
        supabase: Any = None,
        supabase_admin: Any = None,
    ) -> None:
        self._contract = contract
        self._supabase = supabase
        self._supabase_admin = supabase_admin

    # ---- lazy deps -------------------------------------------------------

    @property
    def contract(self) -> P2PEscrowContract:
        if self._contract is None:
            self._contract = get_contract()
        return self._contract

    @property
    def supabase(self) -> Any:
        """Regular client for read operations (respects RLS)."""
        if self._supabase is None:
            from supabase_client import get_supabase_client

            self._supabase = get_supabase_client()
        return self._supabase

    @property
    def admin(self) -> Any:
        """Admin client (service-role) for write operations that bypass RLS.
        
        This is required because the p2p_orders and p2p_trades tables have
        Row Level Security enabled. Regular authenticated users cannot INSERT
        into these tables directly.
        """
        if self._supabase_admin is None:
            from supabase_client import get_supabase_admin_client

            self._supabase_admin = get_supabase_admin_client()
        if self._supabase_admin is None:
            logger.error(
                "Supabase admin client not configured. "
                "Set SUPABASE_SERVICE_ROLE_KEY environment variable. "
                "P2P write operations will fail with RLS errors."
            )
        return self._supabase_admin

    # ---- ad lifecycle ----------------------------------------------------

    def prepare_open_ad(
        self,
        seller_wallet: str,
        total_g_dollar: float,
        min_order_g_dollar: float,
        max_order_g_dollar: float,
        fiat_amount: float,
        fiat_currency: str,
        payment_method: str,
        payment_details: str = "",
        description: str = "",
    ) -> Dict[str, Any]:
        """Create a draft ad row and return the unsigned txs the seller
        must submit (approve G$ then call ``openAd``).
        
        minOrder and maxOrder define the flexible order size range.
        """
        MIN_ORDER = 1_000.0
        
        if total_g_dollar < MIN_ORDER:
            return {
                "success": False,
                "error": f"Minimum ad size is {MIN_ORDER:,.0f} G$",
            }
        if min_order_g_dollar < MIN_ORDER:
            return {
                "success": False,
                "error": f"Minimum order size is {MIN_ORDER:,.0f} G$",
            }
        if max_order_g_dollar < min_order_g_dollar:
            return {
                "success": False,
                "error": "max_order < min_order",
            }
        if max_order_g_dollar > total_g_dollar:
            return {
                "success": False,
                "error": "max_order > total_g_dollar",
            }
        if fiat_currency not in self.fiat_currencies:
            return {
                "success": False,
                "error": f"Currency {fiat_currency} not supported",
            }
        if payment_method not in self.payment_methods:
            return {
                "success": False,
                "error": f"Payment method {payment_method} not supported",
            }
        if fiat_amount <= 0:
            return {"success": False, "error": "Invalid fiat amount"}

        # Sanity: seller actually holds the G$ they're trying to lock.
        try:
            _, balance_gd = self.contract.gd_balance(seller_wallet)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "error": f"Balance check failed: {exc}"}
        if balance_gd < total_g_dollar:
            return {
                "success": False,
                "error": (
                    f"Insufficient G$ balance: have {balance_gd:,.2f}, "
                    f"need {total_g_dollar:,.2f}"
                ),
            }

        ad_id = make_random_bytes32("ad")
        order_id = f"P2P-{uuid.uuid4().hex[:8].upper()}"
        rate = float(fiat_amount) / float(total_g_dollar) if total_g_dollar > 0 else 0

        row = {
            "order_id": order_id,
            "seller_wallet": seller_wallet.lower(),
            "g_dollar_amount": float(total_g_dollar),
            "fiat_amount": float(fiat_amount),
            "fiat_currency": fiat_currency,
            "payment_method": payment_method,
            "payment_details": payment_details,
            "rate": rate,
            "description": description,
            "status": "draft",
            "ad_id_onchain": _hex(ad_id),
            "contract_address": self.contract.address.lower(),
            "chain_id": self.contract.chain_id,
            "total_locked_gd": float(total_g_dollar),
            "remaining_amount_gd": float(total_g_dollar),
            "min_order_gd": float(min_order_g_dollar),
            "max_order_gd": float(max_order_g_dollar),
            "active_trade_count": 0,
            "onchain_status": "pending_user_signature",
            "created_at": _utcnow_iso(),
        }

        # Use admin client to bypass RLS for INSERT operations
        db = self.admin or self.supabase
        try:
            insert = db.table("p2p_orders").insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            logger.exception("p2p_orders insert failed")
            return {"success": False, "error": f"DB insert failed: {exc}"}
        if not insert.data:
            return {"success": False, "error": "Failed to create ad row"}

        approve_tx = self.contract.build_approve_tx(
            seller_wallet, total_g_dollar
        )
        open_ad_tx = self.contract.build_open_ad_tx(
            seller_wallet,
            ad_id,
            total_g_dollar,
            min_order_g_dollar,
            max_order_g_dollar,
        )

        # Allowance hint so the frontend can skip approve() if the seller has
        # already approved enough.
        try:
            current_allowance = self.contract.gd_allowance(seller_wallet)
        except Exception:  # noqa: BLE001
            current_allowance = 0
        approve_needed = current_allowance < _to_wei(total_g_dollar)

        return {
            "success": True,
            "order": insert.data[0],
            "ad_id_onchain": _hex(ad_id),
            "approve_needed": approve_needed,
            "current_allowance_wei": current_allowance,
            "transactions": {
                "approve": approve_tx,
                "open_ad": open_ad_tx,
            },
        }

    def prepare_refund_ad(
        self, seller_wallet: str, order_id: str
    ) -> Dict[str, Any]:
        """Prepare a refund transaction for an ad.
        
        Refunds remaining G$ from ad. Only works if no active trades.
        """
        order = self._fetch_order(order_id)
        if not order:
            return {"success": False, "error": "Order not found"}
        if (order.get("seller_wallet") or "").lower() != seller_wallet.lower():
            return {"success": False, "error": "Not your ad"}
        ad_id_hex = order.get("ad_id_onchain")
        if not ad_id_hex:
            return {
                "success": False,
                "error": "Ad has not been opened on-chain yet",
            }
        
        # Check on-chain state for active trades
        try:
            ad = self.contract.get_ad(ad_id_hex)
            if ad and ad.active_trade_count > 0:
                return {
                    "success": False,
                    "error": f"Cannot refund: {ad.active_trade_count} active trade(s) still pending",
                }
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to check ad state: %s", exc)
        
        refund_tx = self.contract.build_refund_ad_tx(seller_wallet, ad_id_hex)
        
        return {
            "success": True,
            "ad_id_onchain": ad_id_hex,
            "transactions": {
                "refund": refund_tx,
            },
        }

    # ---- order placement ------------------------------------------------

    def prepare_place_order(
        self,
        buyer_wallet: str,
        order_id: str,
        amount_g_dollar: float,
        payment_window_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Prepare a place order transaction for a buyer.
        
        Amount must be between minOrder and maxOrder of the ad.
        """
        MIN_ORDER = 1_000.0
        
        order = self._fetch_order(order_id)
        if not order:
            return {"success": False, "error": "Order not found"}
        if (order.get("onchain_status") or "") != "open":
            return {
                "success": False,
                "error": "Ad is not open for new orders",
            }
        seller = (order.get("seller_wallet") or "").lower()
        if seller == buyer_wallet.lower():
            return {"success": False, "error": "Cannot trade with yourself"}

        ad_id_hex = order.get("ad_id_onchain")
        if not ad_id_hex:
            return {
                "success": False,
                "error": "Ad not opened on-chain yet",
            }
        
        min_order = float(order.get("min_order_gd") or MIN_ORDER)
        max_order = float(order.get("max_order_gd") or 999999999)
        
        if amount_g_dollar < min_order:
            return {
                "success": False,
                "error": f"Amount below minimum order of {min_order:,.0f} G$",
            }
        if amount_g_dollar > max_order:
            return {
                "success": False,
                "error": f"Amount exceeds maximum order of {max_order:,.0f} G$",
            }
        
        # Check remaining amount on-chain
        try:
            ad = self.contract.get_ad(ad_id_hex)
            if not ad or not ad.is_open:
                return {"success": False, "error": "Ad is no longer open"}
            if ad.remaining_amount < _to_wei(amount_g_dollar):
                return {"success": False, "error": "Insufficient remaining amount in ad"}
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to check ad: %s", exc)
        
        trade_id = make_random_bytes32("trade")
        trade_id_hex = _hex(trade_id)
        
        place_order_tx = self.contract.build_place_order_tx(
            buyer_wallet,
            ad_id_hex,
            trade_id,
            amount_g_dollar,
            payment_window_seconds,
        )
        
        # Calculate fiat amount at the ad's rate
        rate = float(order.get("rate") or 0)
        fiat_amount = round(amount_g_dollar * rate, 6)
        
        row = {
            "trade_id": trade_id_hex,
            "order_id": order_id,
            "buyer_wallet": buyer_wallet.lower(),
            "seller_wallet": seller,
            "g_dollar_amount": amount_g_dollar,
            "fiat_amount": fiat_amount,
            "fiat_currency": order.get("fiat_currency"),
            "payment_method": order.get("payment_method"),
            "payment_details": order.get("payment_details"),
            "rate": order.get("rate"),
            "status": "draft",
            "trade_id_onchain": trade_id_hex,
            "chain_id": self.contract.chain_id,
            "onchain_status": "pending_user_signature",
            "created_at": _utcnow_iso(),
        }
        
        db = self.admin or self.supabase
        try:
            insert = db.table("p2p_trades").insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            logger.exception("p2p_trades insert failed")
            return {"success": False, "error": f"DB insert failed: {exc}"}
        
        return {
            "success": True,
            "trade": insert.data[0] if insert.data else None,
            "trade_id_onchain": trade_id_hex,
            "transactions": {
                "place_order": place_order_tx,
            },
            "order_amount_gd": amount_g_dollar,
            "fiat_amount": fiat_amount,
            "payment_deadline": place_order_tx.get("payment_deadline"),
        }

    # ---- proof + state transitions --------------------------------------

    def upload_payment_proof(
        self, buyer_wallet: str, trade_id: str, proof_url: str
    ) -> Dict[str, Any]:
        trade = self._fetch_trade(trade_id)
        if not trade:
            return {"success": False, "error": "Trade not found"}
        if (trade.get("buyer_wallet") or "").lower() != buyer_wallet.lower():
            return {"success": False, "error": "Not your trade"}
        # Don't let the buyer swap the proof on a disputed/completed trade.
        # During an active dispute the arbiter's UI reads this URL directly,
        # so allowing late edits would let the buyer hot-swap evidence.
        if (trade.get("onchain_status") or "") not in (
            "payment_pending",
            "awaiting_release",
        ):
            return {
                "success": False,
                "error": (
                    "Proof can only be uploaded while the trade is "
                    "payment_pending or awaiting_release"
                ),
            }
        if not proof_url:
            return {"success": False, "error": "Missing proof URL"}
        # Reject anything that isn't a plain http(s) URL: the value is later
        # rendered into an <a href="..."> attribute, so a "javascript:" URL
        # or one carrying quotes / angle brackets would let a malicious
        # buyer inject script into the seller's or admin's view.
        proof_url = proof_url.strip()
        if not (
            proof_url.startswith("https://") or proof_url.startswith("http://")
        ):
            return {
                "success": False,
                "error": "Proof URL must start with https:// or http://",
            }
        if any(ch in proof_url for ch in ('"', "'", "<", ">", " ", "\n", "\r", "\t")):
            return {
                "success": False,
                "error": "Proof URL contains invalid characters",
            }
        if len(proof_url) > 1000:
            return {"success": False, "error": "Proof URL too long"}
        # Use admin client to bypass RLS for UPDATE operations
        db = self.admin or self.supabase
        try:
            db.table("p2p_trades").update(
                {
                    "payment_proof_url": proof_url,
                    "payment_proof_uploaded_at": _utcnow_iso(),
                }
            ).eq("trade_id", trade_id).execute()
        except Exception as exc:  # noqa: BLE001
            logger.exception("upload_payment_proof failed")
            return {"success": False, "error": f"DB update failed: {exc}"}
        self._log_action(
            trade_id=trade_id, actor=buyer_wallet, action="proof_uploaded"
        )
        return {"success": True}

    def prepare_mark_paid(
        self, buyer_wallet: str, trade_id: str
    ) -> Dict[str, Any]:
        trade = self._fetch_trade(trade_id)
        if not trade:
            return {"success": False, "error": "Trade not found"}
        if (trade.get("buyer_wallet") or "").lower() != buyer_wallet.lower():
            return {"success": False, "error": "Not your trade"}
        if (trade.get("onchain_status") or "") != "payment_pending":
            return {
                "success": False,
                "error": (
                    "Trade is not in payment_pending; current state="
                    f"{trade.get('onchain_status')}"
                ),
            }
        if not trade.get("payment_proof_url"):
            return {
                "success": False,
                "error": "Upload payment proof before marking paid",
            }
        tx = self.contract.build_mark_paid_tx(
            buyer_wallet, trade["trade_id_onchain"]
        )
        return {
            "success": True,
            "trade": trade,
            "transactions": {"mark_paid": tx},
        }

    def prepare_release(
        self, seller_wallet: str, trade_id: str
    ) -> Dict[str, Any]:
        trade = self._fetch_trade(trade_id)
        if not trade:
            return {"success": False, "error": "Trade not found"}
        if (trade.get("seller_wallet") or "").lower() != seller_wallet.lower():
            return {"success": False, "error": "Not your trade"}
        if (trade.get("onchain_status") or "") != "awaiting_release":
            return {
                "success": False,
                "error": (
                    "Trade is not awaiting release; current state="
                    f"{trade.get('onchain_status')}"
                ),
            }
        tx = self.contract.build_release_tx(
            seller_wallet, trade["trade_id_onchain"]
        )
        return {
            "success": True,
            "trade": trade,
            "transactions": {"release": tx},
        }

    def prepare_cancel_order(
        self, buyer_wallet: str, trade_id: str
    ) -> Dict[str, Any]:
        trade = self._fetch_trade(trade_id)
        if not trade:
            return {"success": False, "error": "Trade not found"}
        if (trade.get("buyer_wallet") or "").lower() != buyer_wallet.lower():
            return {"success": False, "error": "Not your trade"}
        if (trade.get("onchain_status") or "") != "payment_pending":
            return {
                "success": False,
                "error": (
                    "Cannot cancel after marking paid; current state="
                    f"{trade.get('onchain_status')}"
                ),
            }
        tx = self.contract.build_cancel_order_tx(
            buyer_wallet, trade["trade_id_onchain"]
        )
        return {
            "success": True,
            "trade": trade,
            "transactions": {"cancel_order": tx},
        }

    def prepare_dispute(
        self, wallet: str, trade_id: str
    ) -> Dict[str, Any]:
        trade = self._fetch_trade(trade_id)
        if not trade:
            return {"success": False, "error": "Trade not found"}
        wallet = wallet.lower()
        is_buyer = (trade.get("buyer_wallet") or "").lower() == wallet
        is_seller = (trade.get("seller_wallet") or "").lower() == wallet
        if not (is_buyer or is_seller):
            return {"success": False, "error": "Not your trade"}
        if (trade.get("onchain_status") or "") != "awaiting_release":
            return {
                "success": False,
                "error": (
                    "Disputes can only be opened while the trade is "
                    "awaiting_release"
                ),
            }
        if is_buyer:
            tx = self.contract.build_dispute_as_buyer_tx(
                wallet, trade["trade_id_onchain"]
            )
        else:
            tx = self.contract.build_dispute_as_seller_tx(
                wallet, trade["trade_id_onchain"]
            )
        return {
            "success": True,
            "trade": trade,
            "transactions": {"dispute": tx},
        }

    # ---- recording client tx submissions --------------------------------

    def record_tx_submitted(
        self,
        kind: str,
        identifier: str,
        tx_hash: str,
        actor_wallet: str,
    ) -> Dict[str, Any]:
        """Record that the user has submitted a tx_hash for the given action.

        The indexer is the authoritative state source; this just lets us
        log the optimistic step and surface it in the UI ("waiting for
        confirmation"). Idempotent.

        Authorization: only the row owner can record a tx hash, and we
        only allow regressing into ``submitted`` from
        ``pending_user_signature`` so a late-arriving call cannot wipe
        out the indexer's authoritative state.
        """
        actor = (actor_wallet or "").lower()
        if not actor:
            return {"success": False, "error": "Missing actor"}

        # tx_hash is later interpolated into <a href="..."> in the UI; reject
        # anything that isn't a real Ethereum-style tx hash so a malicious
        # actor cannot inject script or break out of the attribute.
        if not isinstance(tx_hash, str) or not _TX_HASH_RE.match(tx_hash):
            return {"success": False, "error": "Invalid tx_hash format"}

        if kind == "ad":
            order = self._fetch_order(identifier)
            if not order:
                return {"success": False, "error": "Order not found"}
            if (order.get("seller_wallet") or "").lower() != actor:
                return {"success": False, "error": "Not your ad"}
            if (order.get("onchain_status") or "") not in ("pending_user_signature", "submitted"):
                # Already advanced by indexer or another actor; ignore.
                return {"success": True, "skipped": True}
            
            ad_id_hex = order.get("ad_id_onchain")
            db = self.admin or self.supabase
            
            # Check blockchain directly to confirm the ad is open
            # This ensures the ad shows immediately even without the indexer
            target_status = "submitted"
            if ad_id_hex:
                try:
                    ad_view = self.contract.get_ad(ad_id_hex)
                    if ad_view and ad_view.open:
                        target_status = "open"  # Confirmed on-chain!
                except Exception:
                    pass  # Fallback to submitted if blockchain check fails
            
            try:
                db.table("p2p_orders").update(
                    {
                        "ad_open_tx": tx_hash,
                        "onchain_status": target_status,
                        "onchain_confirmed_at": _utcnow_iso() if target_status == "open" else None,
                    }
                ).eq("order_id", identifier).eq(
                    "onchain_status", "pending_user_signature"
                ).execute()
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": str(exc)}
        elif kind == "trade":
            trade = self._fetch_trade(identifier)
            if not trade:
                return {"success": False, "error": "Trade not found"}
            owners = {
                (trade.get("buyer_wallet") or "").lower(),
                (trade.get("seller_wallet") or "").lower(),
            }
            if actor not in owners:
                return {"success": False, "error": "Not your trade"}
            if (trade.get("onchain_status") or "") not in ("pending_user_signature", "submitted"):
                return {"success": True, "skipped": True}
            
            trade_id_hex = trade.get("trade_id_onchain")
            db = self.admin or self.supabase
            
            # Check blockchain directly to confirm the trade is active
            target_status = "submitted"
            if trade_id_hex:
                try:
                    trade_view = self.contract.get_trade(trade_id_hex)
                    if trade_view and trade_view.exists:
                        target_status = "payment_pending"  # Confirmed on-chain!
                except Exception:
                    pass  # Fallback to submitted if blockchain check fails
            
            try:
                db.table("p2p_trades").update(
                    {
                        "place_order_tx": tx_hash,
                        "onchain_status": target_status,
                        "onchain_confirmed_at": _utcnow_iso() if target_status != "submitted" else None,
                    }
                ).eq("trade_id", identifier).eq(
                    "onchain_status", "pending_user_signature"
                ).execute()
            except Exception as exc:  # noqa: BLE001
                return {"success": False, "error": str(exc)}
        else:
            return {"success": False, "error": f"Unknown kind: {kind}"}
        self._log_action(
            order_id=identifier if kind == "ad" else None,
            trade_id=identifier if kind == "trade" else None,
            actor=actor_wallet,
            action=f"{kind}_tx_submitted",
            tx_hash=tx_hash,
        )
        return {"success": True}

    # ---- read APIs -------------------------------------------------------

    def list_open_ads(
        self,
        viewer_wallet: Optional[str] = None,
        fiat_currency: Optional[str] = None,
        payment_method: Optional[str] = None,
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        try:
            q = (
                self.supabase.table("p2p_orders")
                .select("*")
                .eq("onchain_status", "open")
            )
            if fiat_currency:
                q = q.eq("fiat_currency", fiat_currency)
            if payment_method:
                q = q.eq("payment_method", payment_method)
            if viewer_wallet:
                q = q.neq("seller_wallet", viewer_wallet.lower())
            res = q.order("created_at", desc=True).limit(limit).execute()
            return res.data or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_open_ads failed: %s", exc)
            return []

    def get_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        return self._fetch_order(order_id)

    def get_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        return self._fetch_trade(trade_id)

    def get_my_ads(self, wallet: str, limit: int = 50) -> List[Dict[str, Any]]:
        try:
            res = (
                self.supabase.table("p2p_orders")
                .select("*")
                .eq("seller_wallet", wallet.lower())
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            # Normalize status: if pending_user_signature but has ad_open_tx, treat as open
            for ad in (res.data or []):
                if ad.get("onchain_status") == "pending_user_signature" and ad.get("ad_open_tx"):
                    ad["onchain_status"] = "open"
                    logger.info(f"Normalized ad {ad.get('order_id')} from pending_user_signature to open")
            return res.data or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_my_ads failed: %s", exc)
            return []

    def get_my_trades(self, wallet: str, limit: int = 50) -> List[Dict[str, Any]]:
        wallet_lower = wallet.lower()
        try:
            buyer = (
                self.supabase.table("p2p_trades")
                .select("*")
                .eq("buyer_wallet", wallet_lower)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            seller = (
                self.supabase.table("p2p_trades")
                .select("*")
                .eq("seller_wallet", wallet_lower)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            seen = set()
            combined: List[Dict[str, Any]] = []
            for t in (buyer.data or []) + (seller.data or []):
                key = t.get("trade_id")
                if key and key not in seen:
                    seen.add(key)
                    # Normalize status: if pending_user_signature but has tx, treat as payment_pending
                    if t.get("onchain_status") == "pending_user_signature" and t.get("place_order_tx"):
                        t["onchain_status"] = "payment_pending"
                        logger.info(f"Normalized trade {key} from pending_user_signature to payment_pending")
                    combined.append(t)
            combined.sort(
                key=lambda r: r.get("created_at") or "", reverse=True
            )
            return combined[:limit]
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_my_trades failed: %s", exc)
            return []

    def get_disputes(self, limit: int = 50) -> List[Dict[str, Any]]:
        """Admin: list trades currently in disputed state for arbiter review."""
        try:
            res = (
                self.supabase.table("p2p_trades")
                .select("*")
                .eq("onchain_status", "disputed")
                .order("disputed_at", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_disputes failed: %s", exc)
            return []

    def resolve_dispute(
        self, trade_id: str, buyer_wins: bool, arbiter_wallet: str
    ) -> Dict[str, Any]:
        trade = self._fetch_trade(trade_id)
        if not trade:
            return {"success": False, "error": "Trade not found"}
        if not trade.get("trade_id_onchain"):
            return {
                "success": False,
                "error": "Trade has no on-chain id",
            }
        # Bail out before signing/broadcasting if the trade isn't actually
        # disputed: the contract would revert and we'd just burn ADMIN_KEY's
        # CELO on gas. Mirrors the state guards on the buyer/seller paths.
        if (trade.get("onchain_status") or "") != "disputed":
            return {
                "success": False,
                "error": (
                    "Trade is not in disputed state; current state="
                    f"{trade.get('onchain_status')}"
                ),
            }
        result = self.contract.send_resolve_dispute(
            trade["trade_id_onchain"], buyer_wins
        )
        if result.get("success"):
            self._log_action(
                trade_id=trade_id,
                actor=arbiter_wallet,
                action="dispute_resolved",
                tx_hash=result.get("tx_hash"),
                notes=("buyer_wins" if buyer_wins else "seller_wins"),
            )
        return result

    def contract_status(self) -> Dict[str, Any]:
        """Get the contract status with graceful error handling.
        
        If the blockchain RPC is unavailable, returns cached/degraded status
        instead of failing entirely, so users can still see basic info.
        """
        status = {
            "address": self.contract.address,
            "chain_id": self.contract.chain_id,
            "g_dollar_token": self.contract.g_dollar_address,
            "deployed_block": self.contract.deployed_block,
            "blockchain_available": True,
            "error": None,
        }
        
        try:
            status["paused"] = self.contract.is_paused()
        except Exception as exc:
            logger.warning("contract_status: is_paused() failed: %s", exc)
            status["paused"] = None  # Unknown state
            status["blockchain_available"] = False
            status["blockchain_error"] = "Unable to verify contract state. Blockchain may be experiencing issues."
        
        return status

    # ---- internals -------------------------------------------------------

    def _fetch_order(self, order_id: str) -> Optional[Dict[str, Any]]:
        try:
            res = (
                self.supabase.table("p2p_orders")
                .select("*")
                .eq("order_id", order_id)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_order(%s) failed: %s", order_id, exc)
            return None

    def _fetch_trade(self, trade_id: str) -> Optional[Dict[str, Any]]:
        try:
            res = (
                self.supabase.table("p2p_trades")
                .select("*")
                .eq("trade_id", trade_id)
                .limit(1)
                .execute()
            )
            return res.data[0] if res.data else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("fetch_trade(%s) failed: %s", trade_id, exc)
            return None

    def _log_action(
        self,
        action: str,
        actor: Optional[str] = None,
        trade_id: Optional[str] = None,
        order_id: Optional[str] = None,
        tx_hash: Optional[str] = None,
        notes: Optional[str] = None,
        amount_gd: Optional[float] = None,
    ) -> None:
        # Use admin client to bypass RLS for INSERT operations
        db = self.admin or self.supabase
        try:
            db.table("p2p_escrow_logs").insert(
                {
                    "action": action,
                    "actor": (actor or "").lower() or None,
                    "trade_id": trade_id,
                    "order_id": order_id,
                    "tx_hash": tx_hash,
                    "amount_gd": amount_gd,
                    "notes": notes,
                    "created_at": _utcnow_iso(),
                }
            ).execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning("log_action failed: %s", exc)

    def backfill_stuck_records(self) -> int:
        """Backfill ads and trades stuck at 'submitted' or 'pending_user_signature'.
        
        Queries the blockchain to verify actual state and updates DB accordingly.
        This fixes records created before the fix was deployed or when 
        reportSubmitted() wasn't called properly.
        
        Returns the count of updated records.
        """
        db = self.admin or self.supabase
        updated_count = 0
        
        try:
            # ===== BACKFILL STUCK ADS =====
            stuck_ads = (
                self.supabase.table("p2p_orders")
                .select("order_id, ad_id_onchain, seller_wallet, onchain_status")
                .in_("onchain_status", ["submitted", "pending_user_signature"])
                .execute()
            )
            
            if stuck_ads.data:
                logger.info(f"Found {len(stuck_ads.data)} stuck ads to backfill")
                
                for ad in stuck_ads.data:
                    ad_id_hex = ad.get("ad_id_onchain")
                    if not ad_id_hex:
                        continue
                    
                    try:
                        ad_view = self.contract.get_ad(ad_id_hex)
                        
                        if ad_view and ad_view.open:
                            db.table("p2p_orders").update({
                                "onchain_status": "open",
                                "total_locked_gd": float(ad_view.total_locked) / 1e18,
                                "remaining_amount_gd": float(ad_view.remaining_amount) / 1e18,
                                "min_order_gd": float(ad_view.min_order) / 1e18,
                                "max_order_gd": float(ad_view.max_order) / 1e18,
                                "onchain_confirmed_at": _utcnow_iso(),
                            }).eq("order_id", ad["order_id"]).execute()
                            
                            self._log_action(
                                action="backfill_confirmed",
                                order_id=ad["order_id"],
                                actor=ad.get("seller_wallet"),
                                notes="Backfilled from stuck status",
                            )
                            updated_count += 1
                            logger.info(f"Backfilled ad {ad['order_id']} -> open")
                        else:
                            db.table("p2p_orders").update({
                                "onchain_status": "closed",
                                "closed_at": _utcnow_iso(),
                            }).eq("order_id", ad["order_id"]).execute()
                            
                            self._log_action(
                                action="backfill_closed",
                                order_id=ad["order_id"],
                                actor=ad.get("seller_wallet"),
                                notes="Marked closed (not found on-chain)",
                            )
                            updated_count += 1
                            logger.info(f"Closed stale ad {ad['order_id']}")
                            
                    except Exception as exc:
                        logger.warning(f"Failed to backfill ad {ad['order_id']}: {exc}")
                        continue
            
            # ===== BACKFILL STUCK TRADES =====
            stuck_trades = (
                self.supabase.table("p2p_trades")
                .select("trade_id, trade_id_onchain, buyer_wallet, seller_wallet, onchain_status")
                .in_("onchain_status", ["submitted", "pending_user_signature"])
                .execute()
            )
            
            if stuck_trades.data:
                logger.info(f"Found {len(stuck_trades.data)} stuck trades to backfill")
                
                for trade in stuck_trades.data:
                    trade_id_hex = trade.get("trade_id_onchain")
                    if not trade_id_hex:
                        continue
                    
                    try:
                        trade_view = self.contract.get_trade(trade_id_hex)
                        
                        if trade_view and trade_view.exists:
                            # Map contract status code to DB status
                            status_map = {
                                0: "payment_pending",
                                1: "payment_pending",
                                2: "awaiting_release",
                                3: "completed",
                                4: "cancelled",
                                5: "expired",
                                6: "disputed",
                                7: "refunded",
                            }
                            new_status = status_map.get(trade_view.status, "payment_pending")
                            
                            db.table("p2p_trades").update({
                                "onchain_status": new_status,
                                "onchain_confirmed_at": _utcnow_iso(),
                            }).eq("trade_id", trade["trade_id"]).execute()
                            
                            self._log_action(
                                action="backfill_confirmed",
                                trade_id=trade["trade_id"],
                                actor=trade.get("buyer_wallet"),
                                notes=f"Backfilled to {new_status}",
                            )
                            updated_count += 1
                            logger.info(f"Backfilled trade {trade['trade_id']} -> {new_status}")
                        else:
                            db.table("p2p_trades").update({
                                "onchain_status": "cancelled",
                                "closed_at": _utcnow_iso(),
                            }).eq("trade_id", trade["trade_id"]).execute()
                            
                            self._log_action(
                                action="backfill_cancelled",
                                trade_id=trade["trade_id"],
                                actor=trade.get("buyer_wallet"),
                                notes="Marked cancelled (not found on-chain)",
                            )
                            updated_count += 1
                            logger.info(f"Cancelled stale trade {trade['trade_id']}")
                            
                    except Exception as exc:
                        logger.warning(f"Failed to backfill trade {trade['trade_id']}: {exc}")
                        continue
            
            logger.info(f"Backfill complete: {updated_count} records updated")
            return updated_count
            
        except Exception as exc:
            logger.exception("Backfill failed: %s", exc)
            raise


# Module-level singleton -----------------------------------------------------
escrow_service = P2PEscrowService()
