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
from typing import Any, Dict, List, Optional

from .contract import (
    P2PEscrowContract,
    TRADE_STATUS_NAMES,
    _from_wei,
    _to_wei,
    get_contract,
    make_random_bytes32,
)

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._contract = contract
        self._supabase = supabase

    # ---- lazy deps -------------------------------------------------------

    @property
    def contract(self) -> P2PEscrowContract:
        if self._contract is None:
            self._contract = get_contract()
        return self._contract

    @property
    def supabase(self) -> Any:
        if self._supabase is None:
            from supabase_client import get_supabase_client

            self._supabase = get_supabase_client()
        return self._supabase

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
        """
        if total_g_dollar < 20_000:
            return {
                "success": False,
                "error": "Minimum ad size is 20,000 G$",
            }
        if min_order_g_dollar < 20_000:
            return {
                "success": False,
                "error": (
                    "Per-trade minimum must be \u2265 20,000 G$ "
                    "(contract MIN_AD_AMOUNT)"
                ),
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
                    f"Insufficient G$ balance: have {balance_gd:.2f}, "
                    f"need {total_g_dollar}"
                ),
            }

        ad_id = make_random_bytes32("ad")
        order_id = f"P2P-{uuid.uuid4().hex[:8].upper()}"
        rate = float(fiat_amount) / float(total_g_dollar)

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

        try:
            insert = self.supabase.table("p2p_orders").insert(row).execute()
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

    def prepare_close_ad(
        self, seller_wallet: str, order_id: str
    ) -> Dict[str, Any]:
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
        if (order.get("active_trade_count") or 0) > 0:
            return {
                "success": False,
                "error": (
                    "Cannot close ad with active trades; resolve or cancel "
                    "all open trades first"
                ),
            }
        # Mirror the state guards on the other prepare_* methods: refuse to
        # build a closeAd tx for an ad that isn't open on-chain (would just
        # revert and burn the seller's gas).
        if (order.get("onchain_status") or "") != "open":
            return {
                "success": False,
                "error": (
                    "Ad is not open on-chain; current state="
                    f"{order.get('onchain_status')}"
                ),
            }
        tx = self.contract.build_close_ad_tx(seller_wallet, ad_id_hex)
        return {
            "success": True,
            "order": order,
            "transactions": {"close_ad": tx},
        }

    # ---- order placement ------------------------------------------------

    def prepare_place_order(
        self,
        buyer_wallet: str,
        order_id: str,
        amount_g_dollar: float,
        payment_window_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
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
                "error": "Ad has no on-chain ID",
            }
        if amount_g_dollar < float(order.get("min_order_gd") or 0):
            return {
                "success": False,
                "error": (
                    f"Amount below ad min ({order.get('min_order_gd')} G$)"
                ),
            }
        if amount_g_dollar > float(order.get("max_order_gd") or 0):
            return {
                "success": False,
                "error": (
                    f"Amount above ad max ({order.get('max_order_gd')} G$)"
                ),
            }
        if amount_g_dollar > float(order.get("remaining_amount_gd") or 0):
            return {
                "success": False,
                "error": "Amount exceeds remaining inventory",
            }

        trade_id = make_random_bytes32("trade")
        trade_id_hex = _hex(trade_id)
        rate = float(order.get("rate") or 0)
        fiat_amount = round(amount_g_dollar * rate, 6) if rate else None

        # Build the unsigned tx FIRST so a payment-window validation error
        # doesn't leave behind an orphan p2p_trades row.
        try:
            place_order_tx = self.contract.build_place_order_tx(
                buyer_wallet,
                ad_id_hex,
                trade_id_hex,
                amount_g_dollar,
                payment_window_seconds,
            )
        except ValueError as exc:
            return {"success": False, "error": str(exc)}

        row = {
            "trade_id": f"TRADE-{uuid.uuid4().hex[:8].upper()}",
            "order_id": order_id,
            "buyer_wallet": buyer_wallet.lower(),
            "seller_wallet": seller,
            "g_dollar_amount": float(amount_g_dollar),
            "fiat_amount": fiat_amount,
            "fiat_currency": order.get("fiat_currency"),
            "payment_method": order.get("payment_method"),
            "rate": rate,
            "status": "draft",
            "trade_id_onchain": trade_id_hex,
            "ad_id_onchain": ad_id_hex,
            "contract_address": self.contract.address.lower(),
            "chain_id": self.contract.chain_id,
            "onchain_status": "pending_user_signature",
            "created_at": _utcnow_iso(),
            "timeout_at": (
                datetime.now(timezone.utc)
                + timedelta(seconds=payment_window_seconds or self.DEFAULT_PAYMENT_WINDOW_SECONDS)
            ).isoformat(),
        }
        try:
            insert = self.supabase.table("p2p_trades").insert(row).execute()
        except Exception as exc:  # noqa: BLE001
            logger.exception("p2p_trades insert failed")
            return {"success": False, "error": f"DB insert failed: {exc}"}
        if not insert.data:
            return {"success": False, "error": "Failed to create trade row"}

        return {
            "success": True,
            "trade": insert.data[0],
            "trade_id_onchain": trade_id_hex,
            "transactions": {"place_order": place_order_tx},
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
        try:
            self.supabase.table("p2p_trades").update(
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
            if (order.get("onchain_status") or "") != "pending_user_signature":
                # Already advanced by indexer or another actor; ignore.
                return {"success": True, "skipped": True}
            try:
                self.supabase.table("p2p_orders").update(
                    {
                        "ad_open_tx": tx_hash,
                        "onchain_status": "submitted",
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
            if (trade.get("onchain_status") or "") != "pending_user_signature":
                return {"success": True, "skipped": True}
            try:
                self.supabase.table("p2p_trades").update(
                    {
                        "place_order_tx": tx_hash,
                        "onchain_status": "submitted",
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
        return {
            "address": self.contract.address,
            "chain_id": self.contract.chain_id,
            "paused": self.contract.is_paused(),
            "g_dollar_token": self.contract.g_dollar_address,
            "deployed_block": self.contract.deployed_block,
        }

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
        try:
            self.supabase.table("p2p_escrow_logs").insert(
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


# Module-level singleton -----------------------------------------------------
escrow_service = P2PEscrowService()
