"""
GoodMarketP2PEscrow contract wrapper — trustless on-chain P2P escrow.

This module is the *only* place in the backend that talks to the
``GoodMarketP2PEscrow`` smart contract. It exposes two kinds of helpers:

1. **Read helpers** (``get_ad``, ``get_trade``, ``next_ad_id``, …) that the
   indexer and the API use to inspect contract state.
2. **Unsigned-transaction builders** (``build_open_ad_tx``,
   ``build_place_order_tx``, ``build_mark_paid_tx``, …) that prepare an
   ``eth_sendTransaction``-style payload for the *user* to sign in the browser
   via WalletConnect / MiniPay. The backend never holds a user's private key
   for these flows.

The only signing key the backend keeps is the ``ADMIN_KEY`` used at deploy
time and for ``resolveDispute`` calls; that single signing path lives in
``send_resolve_dispute()``.

Loading the contract address & ABI:

    contracts/p2p_escrow_deployment.json is the canonical artefact written by
    contracts/deploy_p2p_escrow.py. We read both the address and the ABI from
    it so a redeploy automatically propagates without any extra config.

The token-amount helpers assume G$ has 18 decimals (matches mainnet G$ at
0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A).
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from web3 import Web3
from web3.contract import Contract
from web3.types import TxParams

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Match the deployment artefact's location.
_DEPLOYMENT_PATH = (
    Path(__file__).resolve().parent.parent
    / "contracts"
    / "p2p_escrow_deployment.json"
)

# Minimal ERC20 ABI used to build approve()/balanceOf()/allowance() calls for
# the G$ token. We don't depend on the OpenZeppelin import chain.
ERC20_ABI: List[Dict[str, Any]] = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": True,
        "inputs": [
            {"name": "_owner", "type": "address"},
            {"name": "_spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_spender", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "from", "type": "address"},
            {"indexed": True, "name": "to", "type": "address"},
            {"indexed": False, "name": "value", "type": "uint256"},
        ],
        "name": "Transfer",
        "type": "event",
    },
]

# G$ uses 18 decimals on Celo mainnet.
GD_DECIMALS = 18


# ---------------------------------------------------------------------------
# Lightweight typed views over contract data
# ---------------------------------------------------------------------------


@dataclass
class AdView:
    """In-memory representation of an Ad struct from the contract.

    Mirrors the on-chain ``Ad`` struct in GoodMarketP2PEscrow.sol:
    ``(seller, totalLocked, remainingAmount, minOrder, maxOrder,
      activeTradeCount, open, exists)``.
    """

    ad_id: bytes  # 32-byte ad id
    seller: str
    total_locked: int  # wei
    remaining_amount: int  # wei
    min_order: int
    max_order: int
    active_trade_count: int
    open: bool
    exists: bool

    @property
    def is_open(self) -> bool:
        return self.exists and self.open

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ad_id": "0x" + self.ad_id.hex(),
            "seller": self.seller,
            "total_locked_wei": self.total_locked,
            "total_locked_gd": _from_wei(self.total_locked),
            "remaining_amount_wei": self.remaining_amount,
            "remaining_amount_gd": _from_wei(self.remaining_amount),
            "min_order_wei": self.min_order,
            "min_order_gd": _from_wei(self.min_order),
            "max_order_wei": self.max_order,
            "max_order_gd": _from_wei(self.max_order),
            "active_trade_count": self.active_trade_count,
            "open": self.open,
            "exists": self.exists,
        }


# TradeStatus enum, matching GoodMarketP2PEscrow.sol exactly.
TRADE_STATUS_NONE = 0
TRADE_STATUS_PAYMENT_PENDING = 1
TRADE_STATUS_AWAITING_RELEASE = 2
TRADE_STATUS_COMPLETED = 3
TRADE_STATUS_CANCELLED = 4
TRADE_STATUS_EXPIRED = 5
TRADE_STATUS_DISPUTED = 6
TRADE_STATUS_REFUNDED = 7

TRADE_STATUS_NAMES = {
    0: "none",
    1: "payment_pending",
    2: "awaiting_release",
    3: "completed",
    4: "cancelled",
    5: "expired",
    6: "disputed",
    7: "refunded",
}

_TERMINAL_STATUSES = {
    TRADE_STATUS_COMPLETED,
    TRADE_STATUS_CANCELLED,
    TRADE_STATUS_EXPIRED,
    TRADE_STATUS_REFUNDED,
}


@dataclass
class TradeView:
    """In-memory representation of a Trade struct from the contract.

    Mirrors the on-chain ``Trade`` struct in GoodMarketP2PEscrow.sol:
    ``(adId, buyer, amount, deadline, markedPaidAt, status, exists)``.
    The seller is *not* stored on the trade — look it up via the ad.
    """

    trade_id: bytes
    ad_id: bytes
    buyer: str
    amount: int  # wei
    deadline: int  # epoch seconds, payment-pending expiry
    marked_paid_at: int  # epoch seconds, 0 if never marked paid
    status: int  # see TRADE_STATUS_* constants
    exists: bool

    @property
    def status_name(self) -> str:
        return TRADE_STATUS_NAMES.get(self.status, f"unknown({self.status})")

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def to_dict(self) -> Dict[str, Any]:
        return {
            "trade_id": "0x" + self.trade_id.hex(),
            "ad_id": "0x" + self.ad_id.hex(),
            "buyer": self.buyer,
            "amount_wei": self.amount,
            "amount_gd": _from_wei(self.amount),
            "deadline": self.deadline,
            "marked_paid_at": self.marked_paid_at,
            "status": self.status_name,
            "status_code": self.status,
            "is_terminal": self.is_terminal,
            "exists": self.exists,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _to_wei(amount_gd: float) -> int:
    """Convert a human G$ amount into integer wei (18 decimals).

    Uses string formatting + int() to avoid float-precision drift on round
    numbers like 20000.0 G$.
    """
    if amount_gd < 0:
        raise ValueError("G$ amount must be non-negative")
    return int(round(float(amount_gd) * (10 ** GD_DECIMALS)))


def _from_wei(amount_wei: int) -> float:
    return float(amount_wei) / float(10 ** GD_DECIMALS)


def _checksum(addr: str) -> str:
    return Web3.to_checksum_address(addr)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class P2PEscrowContract:
    """Thin, well-typed wrapper around the deployed ``GoodMarketP2PEscrow``.

    See module docstring for the design split between read helpers,
    unsigned-tx builders, and the (single) admin-signed path.
    """

    # Defaults that match the contract constants
    DEFAULT_PAYMENT_WINDOW_SECONDS = 30 * 60  # 30 min
    MIN_PAYMENT_WINDOW_SECONDS = 15 * 60
    MAX_PAYMENT_WINDOW_SECONDS = 6 * 60 * 60
    AUTO_RELEASE_DELAY_SECONDS = 48 * 60 * 60

    def __init__(self) -> None:
        rpc_url = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
        self.chain_id = int(os.getenv("CHAIN_ID", "42220"))
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))

        self._artifact = self._load_deployment_artifact()
        self.address: str = _checksum(self._artifact["address"])
        self.abi: List[Dict[str, Any]] = self._artifact["abi"]
        self.deployed_block: int = int(self._artifact.get("block_number", 0))

        self.contract: Contract = self.w3.eth.contract(
            address=self.address, abi=self.abi
        )

        self.g_dollar_address: str = _checksum(
            self._artifact.get(
                "g_dollar",
                os.getenv(
                    "GOODDOLLAR_CONTRACT",
                    "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A",
                ),
            )
        )
        self.g_dollar: Contract = self.w3.eth.contract(
            address=self.g_dollar_address, abi=ERC20_ABI
        )

        self._admin_key = os.getenv("ADMIN_KEY")
        if self._admin_key:
            self._admin_account = self.w3.eth.account.from_key(self._admin_key)
            self.admin_address: Optional[str] = self._admin_account.address
        else:
            self._admin_account = None
            self.admin_address = None

        if self.w3.is_connected():
            logger.info(
                "P2PEscrow contract loaded at %s (chain_id=%s, deployed_block=%s)",
                self.address,
                self.chain_id,
                self.deployed_block,
            )
        else:
            logger.error(
                "P2PEscrow contract NOT reachable at %s — RPC %s connect failed",
                self.address,
                rpc_url,
            )

    # ---- artefact loading ------------------------------------------------

    @staticmethod
    def _load_deployment_artifact() -> Dict[str, Any]:
        # Allow override via env (helps tests + alfajores)
        custom_path = os.getenv("P2P_ESCROW_DEPLOYMENT_PATH")
        path = Path(custom_path) if custom_path else _DEPLOYMENT_PATH
        if not path.exists():
            raise FileNotFoundError(
                f"P2PEscrow deployment artefact not found at {path}. "
                "Run contracts/deploy_p2p_escrow.py first."
            )
        with path.open("r") as fh:
            return json.load(fh)

    # ---- read helpers ----------------------------------------------------

    def get_ad(self, ad_id: bytes | str) -> Optional[AdView]:
        ad_id_b = _ensure_bytes32(ad_id)
        try:
            raw = self.contract.functions.ads(ad_id_b).call()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_ad(%s) failed: %s", ad_id_b.hex(), exc)
            return None
        # raw layout matches Ad struct: (seller, totalLocked, remainingAmount,
        # minOrder, maxOrder, activeTradeCount, open, exists)
        exists = bool(raw[7])
        if not exists:
            return None
        return AdView(
            ad_id=ad_id_b,
            seller=_checksum(raw[0]),
            total_locked=int(raw[1]),
            remaining_amount=int(raw[2]),
            min_order=int(raw[3]),
            max_order=int(raw[4]),
            active_trade_count=int(raw[5]),
            open=bool(raw[6]),
            exists=exists,
        )

    def get_trade(self, trade_id: bytes | str) -> Optional[TradeView]:
        trade_id_b = _ensure_bytes32(trade_id)
        try:
            raw = self.contract.functions.trades(trade_id_b).call()
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_trade(%s) failed: %s", trade_id_b.hex(), exc)
            return None
        # raw layout matches Trade struct: (adId, buyer, amount, deadline,
        # markedPaidAt, status, exists)
        exists = bool(raw[6])
        if not exists:
            return None
        return TradeView(
            trade_id=trade_id_b,
            ad_id=bytes(raw[0]),
            buyer=_checksum(raw[1]),
            amount=int(raw[2]),
            deadline=int(raw[3]),
            marked_paid_at=int(raw[4]),
            status=int(raw[5]),
            exists=exists,
        )

    def is_paused(self) -> bool:
        try:
            return bool(self.contract.functions.paused().call())
        except Exception:  # noqa: BLE001
            return False



    def gd_balance(self, wallet: str) -> Tuple[int, float]:
        wei = int(self.g_dollar.functions.balanceOf(_checksum(wallet)).call())
        return wei, _from_wei(wei)

    def gd_allowance(self, owner: str) -> int:
        return int(
            self.g_dollar.functions.allowance(
                _checksum(owner), self.address
            ).call()
        )

    # ---- unsigned tx builders -------------------------------------------

    def build_approve_tx(
        self, owner_wallet: str, amount_gd: float
    ) -> Dict[str, Any]:
        """Build an unsigned `approve(escrow, amount)` tx for the G$ token.

        The seller must approve the escrow before calling ``openAd``. The
        frontend should call ``buildApproveTx`` first, request a wallet sign,
        then call ``buildOpenAdTx``.
        """
        owner = _checksum(owner_wallet)
        amount_wei = _to_wei(amount_gd)
        return self._wrap_tx(
            self.g_dollar.functions.approve(self.address, amount_wei),
            from_=owner,
            label=f"approve({amount_gd} G$)",
        )

    def build_open_ad_tx(
        self,
        seller_wallet: str,
        ad_id: bytes | str,
        total_amount_gd: float,
        min_order_gd: float,
        max_order_gd: float,
    ) -> Dict[str, Any]:
        """Build the unsigned ``openAd`` tx for a seller.

        The contract only stores amounts; the human-readable payment method,
        currency, and metadata stay off-chain in the ``p2p_orders`` row.
        """
        ad_id_b = _ensure_bytes32(ad_id)
        seller = _checksum(seller_wallet)
        return self._wrap_tx(
            self.contract.functions.openAd(
                ad_id_b,
                _to_wei(total_amount_gd),
                _to_wei(min_order_gd),
                _to_wei(max_order_gd),
            ),
            from_=seller,
            label=f"openAd({ad_id_b.hex()})",
        )

    def build_close_ad_tx(
        self, seller_wallet: str, ad_id: bytes | str
    ) -> Dict[str, Any]:
        ad_id_b = _ensure_bytes32(ad_id)
        return self._wrap_tx(
            self.contract.functions.closeAd(ad_id_b),
            from_=_checksum(seller_wallet),
            label=f"closeAd({ad_id_b.hex()})",
        )

    def build_place_order_tx(
        self,
        buyer_wallet: str,
        ad_id: bytes | str,
        trade_id: bytes | str,
        amount_gd: float,
        payment_window_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        ad_id_b = _ensure_bytes32(ad_id)
        trade_id_b = _ensure_bytes32(trade_id)
        if payment_window_seconds is None:
            payment_window_seconds = self.DEFAULT_PAYMENT_WINDOW_SECONDS
        if not (
            self.MIN_PAYMENT_WINDOW_SECONDS
            <= payment_window_seconds
            <= self.MAX_PAYMENT_WINDOW_SECONDS
        ):
            raise ValueError(
                "payment_window_seconds must be between "
                f"{self.MIN_PAYMENT_WINDOW_SECONDS} and "
                f"{self.MAX_PAYMENT_WINDOW_SECONDS}"
            )
        # Add a buffer to absorb the delay between tx preparation and on-chain
        # inclusion (wallet UI render, user signing, propagation, block time).
        # Without it, a user picking the minimum 15-minute window would revert
        # with "P2P: window too short" because by the time the tx is mined,
        # `deadline - block.timestamp` is already < MIN_PAYMENT_WINDOW.
        _DEADLINE_BUFFER_SECONDS = 120
        deadline = (
            int(time.time())
            + int(payment_window_seconds)
            + _DEADLINE_BUFFER_SECONDS
        )
        return self._wrap_tx(
            self.contract.functions.placeOrder(
                ad_id_b,
                trade_id_b,
                _to_wei(amount_gd),
                deadline,
            ),
            from_=_checksum(buyer_wallet),
            label=f"placeOrder({trade_id_b.hex()})",
            extra={"payment_deadline": deadline},
        )

    def build_cancel_order_tx(
        self, buyer_wallet: str, trade_id: bytes | str
    ) -> Dict[str, Any]:
        return self._wrap_tx(
            self.contract.functions.cancelOrder(_ensure_bytes32(trade_id)),
            from_=_checksum(buyer_wallet),
            label="cancelOrder",
        )

    def build_mark_paid_tx(
        self, buyer_wallet: str, trade_id: bytes | str
    ) -> Dict[str, Any]:
        return self._wrap_tx(
            self.contract.functions.markPaid(_ensure_bytes32(trade_id)),
            from_=_checksum(buyer_wallet),
            label="markPaid",
        )

    def build_release_tx(
        self, seller_wallet: str, trade_id: bytes | str
    ) -> Dict[str, Any]:
        return self._wrap_tx(
            self.contract.functions.release(_ensure_bytes32(trade_id)),
            from_=_checksum(seller_wallet),
            label="release",
        )

    def build_dispute_as_buyer_tx(
        self, buyer_wallet: str, trade_id: bytes | str
    ) -> Dict[str, Any]:
        return self._wrap_tx(
            self.contract.functions.disputeAsBuyer(
                _ensure_bytes32(trade_id)
            ),
            from_=_checksum(buyer_wallet),
            label="disputeAsBuyer",
        )

    def build_dispute_as_seller_tx(
        self, seller_wallet: str, trade_id: bytes | str
    ) -> Dict[str, Any]:
        return self._wrap_tx(
            self.contract.functions.disputeAsSeller(
                _ensure_bytes32(trade_id)
            ),
            from_=_checksum(seller_wallet),
            label="disputeAsSeller",
        )

    def build_expire_pending_order_tx(
        self, caller_wallet: str, trade_id: bytes | str
    ) -> Dict[str, Any]:
        return self._wrap_tx(
            self.contract.functions.expirePendingOrder(
                _ensure_bytes32(trade_id)
            ),
            from_=_checksum(caller_wallet),
            label="expirePendingOrder",
        )

    def build_auto_release_tx(
        self, caller_wallet: str, trade_id: bytes | str
    ) -> Dict[str, Any]:
        return self._wrap_tx(
            self.contract.functions.autoReleaseAfterTimeout(
                _ensure_bytes32(trade_id)
            ),
            from_=_checksum(caller_wallet),
            label="autoReleaseAfterTimeout",
        )

    # ---- admin-signed path ----------------------------------------------

    def send_resolve_dispute(
        self, trade_id: bytes | str, buyer_wins: bool
    ) -> Dict[str, Any]:
        """Resolve a dispute using the ADMIN_KEY. Server-signed.

        This is the only place the backend uses ADMIN_KEY for normal
        operations after deployment. ``buyer_wins=True`` releases G$ to the
        buyer; ``False`` refunds the seller.
        """
        if not self._admin_account:
            return {
                "success": False,
                "error": "ADMIN_KEY not configured on backend",
            }
        trade_id_b = _ensure_bytes32(trade_id)
        try:
            fn = self.contract.functions.resolveDispute(trade_id_b, buyer_wins)
            tx_params: TxParams = {
                "from": self.admin_address,
                "chainId": self.chain_id,
                "nonce": self.w3.eth.get_transaction_count(self.admin_address),
            }
            estimated = fn.estimate_gas(tx_params)
            tx_params["gas"] = int(estimated * 1.2)
            tx_params["gasPrice"] = self.w3.eth.gas_price
            tx = fn.build_transaction(tx_params)
            signed = self.w3.eth.account.sign_transaction(tx, self._admin_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            if not tx_hash_hex.startswith("0x"):
                tx_hash_hex = "0x" + tx_hash_hex
            logger.info(
                "resolveDispute(%s, buyer_wins=%s) sent: %s",
                trade_id_b.hex(),
                buyer_wins,
                tx_hash_hex,
            )
            return {
                "success": True,
                "tx_hash": tx_hash_hex,
                "trade_id": "0x" + trade_id_b.hex(),
                "buyer_wins": buyer_wins,
            }
        except Exception as exc:  # noqa: BLE001
            logger.exception("resolveDispute failed")
            return {"success": False, "error": str(exc)}

    # ---- internal --------------------------------------------------------

    def _wrap_tx(
        self,
        fn: Any,
        *,
        from_: str,
        label: str,
        extra: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Build an unsigned tx payload suitable for the frontend wallet.

        We deliberately do NOT include ``nonce`` (the wallet picks the right
        one) or ``gasPrice``/``maxFeePerGas`` (the wallet's fee estimator does
        a better job than us, especially for CIP-64 feeCurrency in G$). We do
        include a generous ``gas`` estimate so MiniPay-style wallets without
        their own estimator can submit.
        """
        try:
            estimated_gas = fn.estimate_gas({"from": from_})
            gas_limit = int(estimated_gas * 1.25)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "gas estimation failed for %s (from=%s): %s — using fallback",
                label,
                from_,
                exc,
            )
            gas_limit = 600_000

        tx = fn.build_transaction(
            {
                "from": from_,
                "chainId": self.chain_id,
                "nonce": 0,  # placeholder; the wallet replaces this
                "gas": gas_limit,
            }
        )
        # Drop the placeholder nonce so frontend wallets don't accidentally
        # try to use it.
        tx.pop("nonce", None)
        # Some web3.py versions emit gasPrice=0; drop it so wallets pick.
        tx.pop("gasPrice", None)

        result: Dict[str, Any] = {
            "to": tx["to"],
            "data": tx["data"] if isinstance(tx["data"], str) else "0x" + tx["data"].hex(),
            "value": hex(tx.get("value", 0)),
            "gas": hex(gas_limit),
            "chainId": hex(self.chain_id),
            "from": from_,
            "label": label,
        }
        if extra:
            result.update(extra)
        return result


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _ensure_bytes32(value: bytes | str) -> bytes:
    if isinstance(value, bytes):
        if len(value) != 32:
            raise ValueError("bytes32 value must be exactly 32 bytes")
        return value
    if isinstance(value, str):
        if value.startswith("0x") or value.startswith("0X"):
            value = value[2:]
        if len(value) != 64:
            raise ValueError(
                f"bytes32 hex must be exactly 64 chars, got {len(value)}"
            )
        return bytes.fromhex(value)
    raise TypeError(f"unsupported bytes32 input type: {type(value)!r}")


def make_random_bytes32(prefix: str = "") -> bytes:
    """Generate a unique bytes32 id by hashing a prefix + random nonce.

    Used to mint ad/trade ids on the backend before showing them to the
    user. The prefix lets us tell ad ids apart from trade ids in on-chain
    logs (purely a debugging convenience — the contract doesn't care).
    """
    nonce = os.urandom(16)
    return Web3.keccak(prefix.encode() + nonce)


# Singleton instance for the rest of the module to import. Lazy so importers
# that only need helpers (e.g. tests) don't pay the cost of a Web3 connection.
_INSTANCE: Optional[P2PEscrowContract] = None


def get_contract() -> P2PEscrowContract:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = P2PEscrowContract()
    return _INSTANCE
