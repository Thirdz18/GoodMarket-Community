"""
P2P escrow blockchain service.

On-chain reads (listing/order state) + the owner-key (P2P_KEY) admin-review
actions (release/refund/resolve), which are the ONLY server-signed fund
movements. All normal buyer/seller actions are user-signed in the frontend
via the contract ABI + GMWalletConnect (see static/js/p2p/*).

Also exposes a CoinGecko price reference for G$ (coin id `gooddollar`).
"""
from env_utils import get_env_float, get_env_int
import os
import logging
import threading
import time

import requests
from web3 import Web3

logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CELO_RPC_URLS = tuple(
    url.strip()
    for url in (
        os.getenv("CELO_RPC_URLS", "")
        or ",".join(
            [
                CELO_RPC_URL,
                "https://1rpc.io/celo",
                "https://celo.publicnode.com",
            ]
        )
    ).split(",")
    if url.strip()
)
CHAIN_ID = get_env_int("CHAIN_ID", 42220)
P2P_ESCROW_CONTRACT_ADDRESS = os.getenv("P2P_ESCROW_CONTRACT_ADDRESS", "")
GD_TOKEN_ADDRESS = os.getenv(
    "GOODDOLLAR_CONTRACT_ADDRESS", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"
)
P2P_KEY = os.getenv("P2P_KEY", "")

MIN_LOCK_GD = 1_000
MAX_LOCK_GD = 1_000_000

# CoinGecko reference price (G$).
COINGECKO_COIN_ID = os.getenv("P2P_COINGECKO_COIN_ID", "gooddollar")
COINGECKO_BASE_URL = os.getenv("COINGECKO_BASE_URL", "https://api.coingecko.com/api/v3")
_PRICE_TTL = 60

ORDER_STATUS = {
    0: "none",
    1: "open",
    2: "paid",
    3: "released",
    4: "cancelled",
    5: "disputed",
}

# ── ABI (subset used by the backend; the frontend ships its own copy) ────────
P2P_ESCROW_ABI = [
    {"inputs": [{"name": "_gdToken", "type": "address"}, {"name": "_paymentWindow", "type": "uint256"}],
     "stateMutability": "nonpayable", "type": "constructor"},
    {"inputs": [], "name": "owner", "outputs": [{"name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "paymentWindow", "outputs": [{"name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "listingId", "type": "uint256"}], "name": "getListing",
     "outputs": [
         {"name": "seller", "type": "address"},
         {"name": "total", "type": "uint256"},
         {"name": "available", "type": "uint256"},
         {"name": "minOrder", "type": "uint256"},
         {"name": "active", "type": "bool"},
     ], "stateMutability": "view", "type": "function"},
    {"inputs": [{"name": "orderId", "type": "uint256"}], "name": "getOrder",
     "outputs": [
         {"name": "listingId", "type": "uint256"},
         {"name": "buyer", "type": "address"},
         {"name": "amount", "type": "uint256"},
         {"name": "createdAt", "type": "uint64"},
         {"name": "deadline", "type": "uint64"},
         {"name": "status", "type": "uint8"},
     ], "stateMutability": "view", "type": "function"},
    # Permissionless after the deadline (anyone may cancel an expired Open order);
    # the auto-expiry worker calls this with P2P_KEY to refund reserved G$ to the seller.
    {"inputs": [{"name": "orderId", "type": "uint256"}], "name": "cancelOrder",
     "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    # Owner-only admin-review actions (server-signed with P2P_KEY).
    {"inputs": [{"name": "orderId", "type": "uint256"}], "name": "releaseOrderByOwner",
     "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "orderId", "type": "uint256"}], "name": "refundOrderByOwner",
     "outputs": [], "stateMutability": "nonpayable", "type": "function"},
    {"inputs": [{"name": "orderId", "type": "uint256"}, {"name": "releaseToBuyer", "type": "bool"}],
     "name": "resolveDispute", "outputs": [], "stateMutability": "nonpayable", "type": "function"},
]

_w3_pool = {}
_w3_lock = threading.Lock()
_price_cache = {}
_price_lock = threading.Lock()


def get_w3():
    with _w3_lock:
        urls = list(CELO_RPC_URLS) or [CELO_RPC_URL]
        for url in urls:
            w3 = _w3_pool.get(url)
            if w3 is None:
                w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": 8}))
                _w3_pool[url] = w3
            try:
                if w3.is_connected():
                    return w3
            except Exception:
                continue
        fallback = urls[0]
        return _w3_pool.setdefault(
            fallback, Web3(Web3.HTTPProvider(fallback, request_kwargs={"timeout": 8}))
        )


def get_escrow_contract(w3):
    if not P2P_ESCROW_CONTRACT_ADDRESS:
        raise ValueError("P2P_ESCROW_CONTRACT_ADDRESS not set")
    return w3.eth.contract(
        address=Web3.to_checksum_address(P2P_ESCROW_CONTRACT_ADDRESS),
        abi=P2P_ESCROW_ABI,
    )


def is_configured():
    return bool(P2P_ESCROW_CONTRACT_ADDRESS)


def _raw_to_gd(raw):
    try:
        return float(int(raw)) / 1e18
    except Exception:
        return 0.0


def get_listing(listing_id):
    """Read an on-chain listing. Returns None if the contract isn't configured."""
    try:
        w3 = get_w3()
        c = get_escrow_contract(w3)
        seller, total, available, min_order, active = c.functions.getListing(int(listing_id)).call()
        if seller == "0x0000000000000000000000000000000000000000":
            return None
        return {
            "onchain_id": int(listing_id),
            "seller": seller,
            "total_raw": str(total),
            "total_gd": _raw_to_gd(total),
            "available_raw": str(available),
            "available_gd": _raw_to_gd(available),
            "min_order_raw": str(min_order),
            "min_order_gd": _raw_to_gd(min_order),
            "active": bool(active),
        }
    except Exception as e:
        logger.error(f"get_listing({listing_id}) error: {e}")
        return None


def get_order(order_id):
    try:
        w3 = get_w3()
        c = get_escrow_contract(w3)
        listing_id, buyer, amount, created_at, deadline, status = c.functions.getOrder(int(order_id)).call()
        if buyer == "0x0000000000000000000000000000000000000000":
            return None
        return {
            "onchain_id": int(order_id),
            "listing_onchain_id": int(listing_id),
            "buyer": buyer,
            "amount_raw": str(amount),
            "amount_gd": _raw_to_gd(amount),
            "created_at": int(created_at),
            "deadline": int(deadline),
            "status": ORDER_STATUS.get(int(status), "unknown"),
            "status_code": int(status),
        }
    except Exception as e:
        logger.error(f"get_order({order_id}) error: {e}")
        return None


def _owner_account(w3):
    if not P2P_KEY:
        raise ValueError("P2P_KEY not set")
    from eth_account import Account

    key = P2P_KEY if P2P_KEY.startswith("0x") else "0x" + P2P_KEY
    return Account.from_key(key), key


def _send_owner_tx(fn_name, *args):
    """Sign and broadcast an owner-only admin action with P2P_KEY."""
    w3 = get_w3()
    c = get_escrow_contract(w3)
    account, key = _owner_account(w3)

    fn = getattr(c.functions, fn_name)(*args)
    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = int(w3.eth.gas_price * 1.2)
    try:
        gas_estimate = fn.estimate_gas({"from": account.address})
    except Exception as e:
        logger.warning(f"{fn_name} estimate_gas failed ({e}); using 300000")
        gas_estimate = 300_000
    tx = fn.build_transaction(
        {
            "chainId": CHAIN_ID,
            "gas": int(gas_estimate * 1.2),
            "gasPrice": gas_price,
            "nonce": nonce,
        }
    )
    signed = w3.eth.account.sign_transaction(tx, key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hash_hex = tx_hash.hex()
    if not tx_hash_hex.startswith("0x"):
        tx_hash_hex = "0x" + tx_hash_hex
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    return {"success": receipt.status == 1, "tx_hash": tx_hash_hex}


def owner_release_order(order_id):
    """Admin review: release escrowed G$ to the buyer (proof verified genuine).

    Seller-rejected orders may already be disputed on-chain if a party clicked
    the dispute action before admin review. The escrow contract's
    releaseOrderByOwner() intentionally only handles Open/Paid orders, while
    Disputed orders must be completed through resolveDispute(orderId, true).
    Pick the correct contract entrypoint up front so admin "Release to buyer"
    works for both normal seller-rejected and disputed review rows.
    """
    order_id = int(order_id)
    onchain_order = get_order(order_id)
    if onchain_order and onchain_order.get("status_code") == 5:
        return _send_owner_tx("resolveDispute", order_id, True)
    return _send_owner_tx("releaseOrderByOwner", order_id)


def owner_refund_order(order_id):
    """Admin review: refund reserved G$ to the seller (proof fake)."""
    return _send_owner_tx("refundOrderByOwner", int(order_id))


def owner_resolve_dispute(order_id, release_to_buyer):
    return _send_owner_tx("resolveDispute", int(order_id), bool(release_to_buyer))


def cancel_expired_order(order_id):
    """
    Cancel an Open order whose payment deadline has passed, returning the
    reserved G$ to the listing's available balance. The contract's cancelOrder
    is permissionless once `block.timestamp > deadline`, so signing with the
    P2P_KEY (or any funded key) works; we reuse P2P_KEY for gas.
    """
    return _send_owner_tx("cancelOrder", int(order_id))


def get_gd_price_reference(fiat_currency=None):
    """
    CoinGecko reference price for G$ (coin id `gooddollar`). Cached ~60s.
    Returns {usd: float, <fiat>: float|None}. Reference/hint only — sellers set
    their own price.
    """
    fiat = (fiat_currency or "").strip().lower()
    cache_key = f"gd_price:{fiat or 'usd'}"
    with _price_lock:
        cached = _price_cache.get(cache_key)
        if cached and time.time() < cached[1]:
            return cached[0]

    vs = "usd"
    if fiat and fiat != "usd":
        vs = f"usd,{fiat}"
    try:
        resp = requests.get(
            f"{COINGECKO_BASE_URL}/simple/price",
            params={"ids": COINGECKO_COIN_ID, "vs_currencies": vs},
            timeout=8,
        )
        resp.raise_for_status()
        data = resp.json().get(COINGECKO_COIN_ID, {})
        result = {
            "coin_id": COINGECKO_COIN_ID,
            "usd": data.get("usd"),
            "fiat_currency": fiat.upper() if fiat else None,
            "fiat": data.get(fiat) if fiat and fiat != "usd" else None,
            "source": "coingecko",
        }
        with _price_lock:
            _price_cache[cache_key] = (result, time.time() + _PRICE_TTL)
        return result
    except Exception as e:
        logger.error(f"get_gd_price_reference error: {e}")
        return {"coin_id": COINGECKO_COIN_ID, "usd": None, "fiat": None, "source": "coingecko", "error": str(e)}
