from env_utils import get_env_float, get_env_int
import os
import logging
import requests
import time
from datetime import datetime
from web3 import Web3
from eth_account import Account
from supabase_client import get_supabase_client

logger = logging.getLogger(__name__)

# G$ token decimals on Celo
GD_DECIMALS = 18
GD_TOKEN_CONTRACT = os.getenv("GOODDOLLAR_CONTRACT", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A")
CELO_RPC_URL = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CHAIN_ID = get_env_int("CHAIN_ID", 42220)

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]

TRANSFER_EVENT_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


def sanitize_error(e: Exception) -> str:
    """Convert raw exceptions (including HTTP errors with URLs) to clean user messages."""
    msg = str(e)
    # Strip raw HTTP error details — "400 Client Error: ... for url: https://..."
    if "Client Error" in msg or "Server Error" in msg or "for url:" in msg or "reloadly.com" in msg or "HTTP" in msg:
        return "Service temporarily unavailable. Please try again later or contact t.me/GoodDollarX"
    if len(msg) > 200:
        return "An error occurred. Please try again later or contact t.me/GoodDollarX"
    return msg


_gd_price_cache: dict = {"price": None, "ts": 0}
_GD_PRICE_CACHE_SECS = 300  # refresh every 5 minutes


def get_gd_usd_price() -> float:
    """Get current G$ price in USD — live from CoinGecko, cached 5 min."""
    env_price = os.getenv("GD_USD_PRICE")
    if env_price:
        try:
            return float(env_price)
        except ValueError:
            pass

    now = time.time()
    if _gd_price_cache["price"] and now - _gd_price_cache["ts"] < _GD_PRICE_CACHE_SECS:
        return _gd_price_cache["price"]

    try:
        resp = requests.get(
            "https://api.coingecko.com/api/v3/simple/price",
            params={"ids": "gooddollar", "vs_currencies": "usd"},
            timeout=8
        )
        if resp.status_code == 429:
            logger.warning("⚠️ CoinGecko rate-limited — using cached/fallback price")
            return _gd_price_cache["price"] or 0.00012
        resp.raise_for_status()
        data = resp.json()
        price = float(data["gooddollar"]["usd"])
        if price > 0:
            _gd_price_cache["price"] = price
            _gd_price_cache["ts"] = now
            logger.info(f"✅ CoinGecko G$ price: ${price:.8f}")
            return price
    except Exception as e:
        logger.warning(f"⚠️ CoinGecko price fetch failed: {e}")

    # Return cached value (even if stale) or fallback
    return _gd_price_cache["price"] or 0.00012


def usd_to_gd(usd_amount: float) -> float:
    """Convert USD amount to G$ amount"""
    price = get_gd_usd_price()
    return round(usd_amount / price, 2) if price > 0 else 0


def gd_to_usd(gd_amount: float) -> float:
    """Convert G$ amount to USD"""
    price = get_gd_usd_price()
    return round(gd_amount * price, 4)


def auto_detect_gd_payment(wallet_address: str, expected_amount_gd: float, hours_back: int = 1) -> dict:
    """
    Automatically scan recent Celo blocks for a G$ Transfer event
    from wallet_address to MERCHANT_ADDRESS with matching amount.
    No tx_hash needed — backend finds it automatically.
    """
    try:
        merchant_address = os.getenv("MERCHANT_ADDRESS")
        if not merchant_address:
            return {"success": False, "error": "MERCHANT_ADDRESS not configured"}

        w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
        if not w3.is_connected():
            return {"success": False, "error": "Cannot connect to Celo network"}

        merchant_checksum = Web3.to_checksum_address(merchant_address)
        sender_checksum = Web3.to_checksum_address(wallet_address)
        token_checksum = Web3.to_checksum_address(GD_TOKEN_CONTRACT)

        current_block = w3.eth.get_block('latest')
        blocks_per_hour = 720  # Celo ~5 sec blocks
        from_block = current_block['number'] - (hours_back * blocks_per_hour)

        # Pad addresses for topic filter (indexed ERC-20 topics)
        padded_from = "0x" + "0" * 24 + sender_checksum.lower().replace("0x", "")
        padded_to = "0x" + "0" * 24 + merchant_checksum.lower().replace("0x", "")

        filter_params = {
            "fromBlock": hex(from_block),
            "toBlock": "latest",
            "address": token_checksum,
            "topics": [TRANSFER_EVENT_SIG, padded_from, padded_to]
        }

        logs = w3.eth.get_logs(filter_params)
        logger.info(f"🔍 Auto-detect: scanning {len(logs)} logs from block {from_block} for {wallet_address[:10]}…")

        tolerance = max(expected_amount_gd * 0.002, 0.02)  # 0.2% or min 0.02 G$

        for log in logs:
            try:
                raw_data = log["data"]
                if isinstance(raw_data, bytes):
                    amount_wei = int(raw_data.hex(), 16)
                elif isinstance(raw_data, str):
                    hex_data = raw_data[2:] if raw_data.startswith("0x") else raw_data
                    amount_wei = int(hex_data, 16)
                else:
                    continue

                actual_gd = amount_wei / (10 ** GD_DECIMALS)

                if abs(actual_gd - expected_amount_gd) > tolerance:
                    continue

                # Get tx hash
                tx_raw = log["transactionHash"]
                tx_hash = ("0x" + tx_raw.hex()) if isinstance(tx_raw, bytes) else tx_raw.hex()
                if not tx_hash.startswith("0x"):
                    tx_hash = "0x" + tx_hash

                logger.info(f"✅ Auto-detected payment: {actual_gd} G$ in tx {tx_hash[:18]}…")
                return {
                    "success": True,
                    "verified": True,
                    "tx_hash": tx_hash,
                    "amount_gd": actual_gd,
                    "block_number": log["blockNumber"]
                }

            except Exception as e:
                logger.warning(f"⚠️ auto_detect log decode error: {e}")
                continue

        logger.info(f"⏳ Auto-detect: no matching transfer found yet for {wallet_address[:10]}… (expected {expected_amount_gd} G$, tol ±{tolerance})")
        return {"success": False, "verified": False, "error": "No matching G$ transfer found yet"}

    except Exception as e:
        logger.error(f"❌ auto_detect_gd_payment error: {e}")
        return {"success": False, "error": str(e)}


def verify_gd_payment(wallet_address: str, expected_amount_gd: float, tx_hash: str) -> dict:
    """
    Verify that a user sent expected_amount_gd G$ to MERCHANT_ADDRESS in tx_hash.
    Returns dict with success bool and details.
    """
    try:
        merchant_address = os.getenv("MERCHANT_ADDRESS")
        if not merchant_address:
            return {"success": False, "error": "MERCHANT_ADDRESS not configured"}

        w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
        if not w3.is_connected():
            return {"success": False, "error": "Cannot connect to Celo network"}

        merchant_checksum = Web3.to_checksum_address(merchant_address)
        token_checksum = Web3.to_checksum_address(GD_TOKEN_CONTRACT)
        sender_checksum = Web3.to_checksum_address(wallet_address)

        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if not receipt:
            return {"success": False, "error": "Transaction not yet mined", "pending": True}
        if receipt.status != 1:
            # Transaction was mined but reverted — definitive failure
            return {"success": False, "error": "Transaction failed on-chain (reverted)", "reverted": True, "tx_found": True}

        for log in receipt.logs:
            if log["address"].lower() != token_checksum.lower():
                continue
            topics = log["topics"]
            if len(topics) < 3:
                continue
            event_sig = topics[0].hex() if not isinstance(topics[0], str) else topics[0]
            if not event_sig.lower().startswith(TRANSFER_EVENT_SIG[:10].lower()):
                continue

            from_addr = "0x" + (topics[1].hex() if not isinstance(topics[1], str) else topics[1])[-40:]
            to_addr = "0x" + (topics[2].hex() if not isinstance(topics[2], str) else topics[2])[-40:]

            if from_addr.lower() != sender_checksum.lower():
                continue
            if to_addr.lower() != merchant_checksum.lower():
                continue

            raw_data = log["data"]
            if isinstance(raw_data, bytes):
                amount_wei = int(raw_data.hex(), 16)
            else:
                amount_wei = int(raw_data, 16) if raw_data.startswith("0x") else int(raw_data, 16)

            actual_gd = amount_wei / (10 ** GD_DECIMALS)
            tolerance = max(0.01, expected_amount_gd * 0.01)

            if abs(actual_gd - expected_amount_gd) <= tolerance:
                return {
                    "success": True,
                    "verified": True,
                    "amount_gd": actual_gd,
                    "tx_hash": tx_hash
                }

        return {"success": False, "error": "No matching G$ transfer found in transaction"}

    except Exception as e:
        logger.error(f"❌ verify_gd_payment error: {e}")
        return {"success": False, "error": str(e)}


def refund_gd(to_wallet: str, amount_gd: float, order_id: str) -> dict:
    """
    Send G$ refund from REFUND_KEY wallet to user.
    Returns dict with success bool and tx_hash.
    """
    try:
        refund_key = os.getenv("REFUND_KEY")
        if not refund_key:
            return {"success": False, "error": "REFUND_KEY not configured"}

        if not refund_key.startswith("0x"):
            refund_key = "0x" + refund_key

        w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
        if not w3.is_connected():
            return {"success": False, "error": "Cannot connect to Celo network"}

        refund_account = Account.from_key(refund_key)
        token_contract = w3.eth.contract(
            address=Web3.to_checksum_address(GD_TOKEN_CONTRACT),
            abi=ERC20_ABI
        )

        recipient = Web3.to_checksum_address(to_wallet)
        # BigInt-safe: avoid float64 precision loss on large G$ amounts
        amount_wei = int(round(float(amount_gd) * (10 ** GD_DECIMALS)))

        nonce = w3.eth.get_transaction_count(refund_account.address)
        gas_price = w3.eth.gas_price

        tx = token_contract.functions.transfer(recipient, amount_wei).build_transaction({
            "chainId": CHAIN_ID,
            "gas": 250000,
            "gasPrice": gas_price,
            "nonce": nonce,
            "from": refund_account.address
        })

        signed = w3.eth.account.sign_transaction(tx, private_key=refund_account.key)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)

        if receipt.status == 1:
            tx_hex = "0x" + tx_hash.hex() if not tx_hash.hex().startswith("0x") else tx_hash.hex()
            logger.info(f"✅ Refund sent: {amount_gd} G$ to {to_wallet} — tx: {tx_hex}")
            return {"success": True, "tx_hash": tx_hex}
        else:
            return {"success": False, "error": "Refund transaction reverted on-chain"}

    except Exception as e:
        logger.error(f"❌ refund_gd error for order {order_id}: {e}")
        return {"success": False, "error": str(e)}


def create_order_record(data: dict) -> dict:
    """Insert a new order record into Supabase"""
    try:
        supabase = get_supabase_client()
        result = supabase.table("reloadly_orders").insert(data).execute()
        return {"success": True, "data": result.data[0] if result.data else {}}
    except Exception as e:
        logger.error(f"❌ create_order_record error: {e}")
        return {"success": False, "error": str(e)}


def update_order_record(order_id: str, updates: dict) -> dict:
    """Update an existing order record"""
    try:
        supabase = get_supabase_client()
        result = supabase.table("reloadly_orders").update(updates).eq("id", order_id).execute()
        return {"success": True, "data": result.data[0] if result.data else {}}
    except Exception as e:
        logger.error(f"❌ update_order_record error: {e}")
        return {"success": False, "error": str(e)}


def get_order_record(order_id: str) -> dict:
    """Get an order record by ID"""
    try:
        supabase = get_supabase_client()
        result = supabase.table("reloadly_orders").select("*").eq("id", order_id).execute()
        if result.data:
            return {"success": True, "order": result.data[0]}
        return {"success": False, "error": "Order not found"}
    except Exception as e:
        logger.error(f"❌ get_order_record error: {e}")
        return {"success": False, "error": str(e)}


def get_user_orders(wallet_address: str, limit: int = 20) -> list:
    """Get order history for a wallet"""
    try:
        supabase = get_supabase_client()
        result = (
            supabase.table("reloadly_orders")
            .select("*")
            .eq("wallet_address", wallet_address.lower())
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return result.data or []
    except Exception as e:
        logger.error(f"❌ get_user_orders error: {e}")
        return []
