
import requests
from datetime import datetime, timedelta, timezone
import logging
import os
import threading

logger = logging.getLogger("blockchain")

CELO_CHAIN_ID = int(os.getenv("CHAIN_ID", "42220"))
CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")

GOODDOLLAR_CONTRACTS = {
    "UBI_PROXY": os.getenv("UBI_PROXY_CONTRACT", "0x43d72Ff17701B2DA814620735C39C620Ce0ea4A1"),
    "UBI_IMPLEMENTATION": "",
    "GOODDOLLAR_TOKEN": os.getenv("GOODDOLLAR_TOKEN_CONTRACT", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"),
    "IDENTITY": os.getenv("IDENTITY_CONTRACT", "0xC361A6E67822a0EDc17D899227dd9FC50BD62F42"),
}

CUSD_CONTRACT = os.getenv("CUSD_CONTRACT", "0x765DE816845861e75A25fCA122bb6898B8B1282a")
USDT_CONTRACT = os.getenv("USDT_CONTRACT", "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")

IDENTITY_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "_account", "type": "address"}],
        "name": "isWhitelisted",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "_account", "type": "address"}],
        "name": "dateAuthenticated",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "address", "name": "_account", "type": "address"}],
        "name": "lastAuthenticated",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "authenticationPeriod",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

UBI_SCHEME_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "_member", "type": "address"}],
        "name": "checkEntitlement",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "claim",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    }
]

_identity_cache: dict = {}
_identity_cache_lock = threading.Lock()
IDENTITY_CACHE_TTL = 1800  # 30 minutes — identity whitelist rarely changes

# G$ USD price cache — refreshed every 30 minutes from CoinGecko
_gd_price_cache: dict = {"price": None, "expires_at": 0}
_gd_price_lock = threading.Lock()

def _get_gd_usd_price() -> float:
    """Return the current G$ price in USD. Uses env var if set, else fetches from CoinGecko.

    Timeout is intentionally short (2s) so this never dominates the wallet balance
    critical path. On timeout/error we fall back to the last cached value (or 0)
    and shorten the cache to retry on the next request.
    """
    import time
    env_price = float(os.getenv("GD_USD_PRICE", "0"))
    if env_price > 0:
        return env_price
    with _gd_price_lock:
        if _gd_price_cache["price"] is not None and _gd_price_cache["expires_at"] > time.time():
            return _gd_price_cache["price"]
        try:
            resp = requests.get(
                "https://api.coingecko.com/api/v3/simple/price",
                params={"ids": "gooddollar", "vs_currencies": "usd"},
                timeout=2
            )
            if resp.status_code == 200:
                price = resp.json().get("gooddollar", {}).get("usd", 0) or 0
                _gd_price_cache["price"] = float(price)
                _gd_price_cache["expires_at"] = time.time() + 1800
                return float(price)
        except Exception:
            pass
        # Fallback: keep any previously cached price so we don't show $0 on a
        # transient CoinGecko hiccup. Shorten TTL so we retry sooner.
        last_price = _gd_price_cache["price"] if _gd_price_cache["price"] is not None else 0.0
        _gd_price_cache["price"] = last_price
        _gd_price_cache["expires_at"] = time.time() + 300
        return last_price

# Shared Web3 instance — reuse TCP connection instead of creating one per request
_w3_singleton = None
_w3_lock = threading.Lock()

def _get_w3():
    global _w3_singleton
    with _w3_lock:
        if _w3_singleton is None or not _w3_singleton.is_connected():
            from web3 import Web3 as _W3
            _w3_singleton = _W3(_W3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 10}))
        return _w3_singleton

# Balance cache: {wallet_lower: {"gd": {...}, "celo": {...}, "expires_at": float}}
_balance_cache: dict = {}
_balance_cache_lock = threading.Lock()
BALANCE_CACHE_TTL = 120  # 2 minutes

# Transfer history cache: {wallet_lower: {"transfers": [...], "expires_at": float}}
_transfer_history_cache: dict = {}
_transfer_history_cache_lock = threading.Lock()
TRANSFER_HISTORY_CACHE_TTL = 300  # 5 minutes

UBI_EVENT_SIGNATURES = {
    "TRANSFER": "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
    "UBI_CLAIMED": "0x89ed24731df6b066e4c5186901fffdba18cd9a10f07494aff900bdee260d1304",
    "UBI_CALCULATED": "0x836fa39995340265746dfe9587d9fe5c5de35b7bce778afd9b124ce1cfeafdc4",
    "UBI_CYCLE_CALCULATED": "0x83e0d535b9e84324e0a25922406398d6ff5f96d0c686204ee490e16d7670566f",
}

CUTOFF_HOURS = 24

log = logging.getLogger("blockchain")

# Shared requests session for connection pooling (reuse TCP connections)
_rpc_session = requests.Session()
_rpc_session.headers.update({"Content-Type": "application/json"})
# Increase pool size so concurrent threads don't wait for a free connection
_http_adapter = requests.adapters.HTTPAdapter(
    pool_connections=10,
    pool_maxsize=20,
    max_retries=requests.adapters.Retry(
        total=3,
        backoff_factor=0.3,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["POST"],
    ),
)
_rpc_session.mount("https://", _http_adapter)
_rpc_session.mount("http://", _http_adapter)
_session_lock = threading.Lock()

# Simple in-memory UBI verification cache
# Format: {wallet_lower: {"result": {...}, "expires_at": float}}
_ubi_cache: dict = {}
_ubi_cache_lock = threading.Lock()
UBI_CACHE_TTL = 900  # 15 minutes — reduces repeated RPC calls on re-login

# Cache for latest block number (30 second TTL)
_block_cache = {"block": 0, "expires_at": 0.0}
_block_cache_lock = threading.Lock()

# Cache for block timestamps — block timestamps are immutable, cache for 6 hours
_block_ts_cache: dict = {}
_block_ts_cache_lock = threading.Lock()
BLOCK_TS_CACHE_TTL = 21600  # 6 hours


def _get_rpc_session() -> requests.Session:
    return _rpc_session


def _get_ubi_cached(wallet_address: str):
    """Return cached UBI result or None if expired/missing."""
    key = wallet_address.lower()
    with _ubi_cache_lock:
        entry = _ubi_cache.get(key)
        if entry and entry["expires_at"] > datetime.now().timestamp():
            logger.info(f"✅ UBI cache HIT for {wallet_address[:8]}...")
            return entry["result"]
    return None


def _set_ubi_cached(wallet_address: str, result: dict):
    """Store UBI result in cache."""
    key = wallet_address.lower()
    import time
    with _ubi_cache_lock:
        _ubi_cache[key] = {
            "result": result,
            "expires_at": time.time() + UBI_CACHE_TTL
        }
        # Keep cache small — remove oldest entries if over 500 wallets
        if len(_ubi_cache) > 500:
            oldest = min(_ubi_cache, key=lambda k: _ubi_cache[k]["expires_at"])
            del _ubi_cache[oldest]


def _topic_for_address(wallet: str) -> str:
    return "0x" + ("0" * 24) + wallet.lower().replace("0x", "")


def _format_timestamp(block_number: int) -> str:
    import time as _time
    # Block timestamps are immutable — check cache first to avoid redundant RPC calls
    with _block_ts_cache_lock:
        entry = _block_ts_cache.get(block_number)
        if entry and entry["expires_at"] > _time.time():
            return entry["formatted"]

    try:
        session = _get_rpc_session()
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getBlockByNumber",
            "params": [hex(block_number), False],
            "id": 1
        }
        response = session.post(CELO_RPC, json=payload, timeout=8)
        result = response.json()

        if "result" in result and result["result"]:
            timestamp = int(result["result"]["timestamp"], 16)
            block_time = datetime.fromtimestamp(timestamp, tz=timezone.utc)
            now = datetime.now(timezone.utc)
            diff = now - block_time

            if diff.days > 0:
                relative = f"{diff.days}d ago"
            elif diff.seconds > 3600:
                hours = diff.seconds // 3600
                relative = f"{hours}h ago"
            else:
                minutes = diff.seconds // 60
                relative = f"{minutes}m ago"

            exact_time = block_time.strftime("%b %d %Y %H:%M:%S %p (+00:00 UTC)")
            formatted = f"{relative} | {exact_time}"

            with _block_ts_cache_lock:
                _block_ts_cache[block_number] = {
                    "formatted": formatted,
                    "expires_at": _time.time() + BLOCK_TS_CACHE_TTL
                }
                # Keep cache size bounded
                if len(_block_ts_cache) > 2000:
                    oldest = min(_block_ts_cache, key=lambda k: _block_ts_cache[k]["expires_at"])
                    del _block_ts_cache[oldest]

            return formatted
    except Exception as e:
        logger.error(f"Error formatting timestamp for block {block_number}: {e}")

    return f"Block #{block_number}"


def _get_latest_block_number() -> int:
    import time
    with _block_cache_lock:
        now = time.time()
        if _block_cache["block"] > 0 and _block_cache["expires_at"] > now:
            return _block_cache["block"]

    try:
        session = _get_rpc_session()
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_blockNumber",
            "params": [],
            "id": 1
        }
        response = session.post(CELO_RPC, json=payload, timeout=8)
        result = response.json()
        block = int(result["result"], 16)

        import time
        with _block_cache_lock:
            _block_cache["block"] = block
            _block_cache["expires_at"] = time.time() + 30  # Cache for 30 seconds

        return block
    except Exception as e:
        logger.error(f"Error getting latest block: {e}")
        return 0


def _calculate_block_range(hours_back: int) -> tuple:
    blocks_per_hour = 720
    latest_block = _get_latest_block_number()
    from_block = latest_block - (hours_back * blocks_per_hour)

    logger.info(f"Block range: {from_block} to {latest_block} (last {hours_back} hours)")
    return hex(from_block), hex(latest_block)


def _batch_rpc_call(payloads: list) -> list:
    """Send multiple JSON-RPC calls in a single HTTP request (batch RPC)."""
    try:
        session = _get_rpc_session()
        response = session.post(CELO_RPC, json=payloads, timeout=20)
        results = response.json()
        if isinstance(results, list):
            return results
        # Single error object returned (e.g. block range too large)
        if isinstance(results, dict) and "error" in results:
            logger.error(f"Batch RPC returned error: {results['error']}")
        return []
    except Exception as e:
        logger.error(f"Batch RPC error: {e}")
        return []


def _get_logs_chunked(params_template: dict, from_block_int: int, to_block_int: int,
                      chunk_size: int = 1000) -> list:
    """
    Call eth_getLogs in chunks to stay within public RPC block-range limits.
    Searches from newest to oldest; stops early on the FIRST chunk that returns
    results (suitable for "has_recent_claim" style checks).
    Returns a flat list of log entries.
    """
    session = _get_rpc_session()
    all_logs = []
    chunk_id = 0

    current_to = to_block_int
    while current_to >= from_block_int:
        current_from = max(from_block_int, current_to - chunk_size + 1)
        chunk_id += 1

        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{**params_template, "fromBlock": hex(current_from), "toBlock": hex(current_to)}],
            "id": chunk_id
        }

        try:
            response = session.post(CELO_RPC, json=payload, timeout=15)
            result = response.json()

            if isinstance(result, dict):
                if "error" in result:
                    logger.error(f"eth_getLogs chunk error (blocks {current_from}-{current_to}): {result['error']}")
                elif "result" in result:
                    logs = result["result"] or []
                    all_logs.extend(logs)
                    if logs:
                        # Found something — stop searching older blocks
                        logger.info(f"Found {len(logs)} logs in chunk {current_from}-{current_to}, stopping early")
                        break
        except Exception as e:
            logger.error(f"eth_getLogs chunk exception (blocks {current_from}-{current_to}): {e}")

        current_to = current_from - 1

    return all_logs


def _get_logs_full(params_template: dict, from_block_int: int, to_block_int: int,
                   chunk_size: int = 10000, batch_size: int = 10) -> list:
    """
    Like _get_logs_chunked but scans the ENTIRE block range without early-stopping.
    Sends multiple chunk requests as a single JSON-RPC batch (up to batch_size at once)
    to minimise round-trips.  Uses a larger default chunk_size for efficiency.
    """
    # Build the list of (from, to) block ranges
    chunks = []
    current_to = to_block_int
    while current_to >= from_block_int:
        current_from = max(from_block_int, current_to - chunk_size + 1)
        chunks.append((current_from, current_to))
        current_to = current_from - 1

    session  = _get_rpc_session()
    all_logs = []

    # Send chunks in batches to keep individual HTTP requests manageable
    for batch_start in range(0, len(chunks), batch_size):
        sub_chunks = chunks[batch_start: batch_start + batch_size]
        batch_payload = [
            {
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [{**params_template,
                            "fromBlock": hex(cf),
                            "toBlock":   hex(ct)}],
                "id": batch_start + idx,
            }
            for idx, (cf, ct) in enumerate(sub_chunks)
        ]
        try:
            response = session.post(CELO_RPC, json=batch_payload, timeout=20)
            results  = response.json()
            # Response may be a list (batch) or a single dict (error)
            if isinstance(results, list):
                for item in results:
                    logs = item.get("result") or []
                    all_logs.extend(logs)
            elif isinstance(results, dict) and "result" in results:
                all_logs.extend(results["result"] or [])
        except Exception as exc:
            logger.error(f"_get_logs_full batch error (chunks {batch_start}–{batch_start+len(sub_chunks)}): {exc}")

    return all_logs


def has_recent_ubi_claim(wallet_address: str) -> dict:
    # Check cache first — avoids ALL blockchain calls
    cached_result = _get_ubi_cached(wallet_address)
    if cached_result is not None:
        return cached_result

    try:
        # Search last 7 days (extended for better detection, CUTOFF_HOURS=24 defines the requirement)
        search_hours = max(CUTOFF_HOURS, 24 * 7)
        from_block, to_block = _calculate_block_range(search_hours)

        ubi_proxy_address = GOODDOLLAR_CONTRACTS["UBI_PROXY"]
        gooddollar_token = GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"]

        all_activities = []

        # --- Check for G$ transfers FROM UBI Proxy TO user ---
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_getLogs",
            "params": [{
                "fromBlock": from_block,
                "toBlock": to_block,
                "address": gooddollar_token,
                "topics": [
                    UBI_EVENT_SIGNATURES["TRANSFER"],
                    _topic_for_address(ubi_proxy_address),
                    _topic_for_address(wallet_address)
                ]
            }],
            "id": 1
        }

        try:
            session = _get_rpc_session()
            response = session.post(CELO_RPC, json=payload, timeout=15)
            result = response.json()

            if "error" not in result:
                logs = result.get("result", [])
                for log_entry in logs:
                    block_num = int(log_entry.get("blockNumber", "0x0"), 16)
                    tx_hash = log_entry.get("transactionHash", "Unknown")
                    timestamp_info = _format_timestamp(block_num)
                    amount_hex = log_entry.get("data", "0x0")
                    try:
                        amount_g = int(amount_hex, 16) / (10 ** 18)
                    except Exception:
                        amount_g = 0
                    all_activities.append({
                        "contract": "UBI Proxy",
                        "contract_address": ubi_proxy_address,
                        "block": block_num,
                        "tx_hash": tx_hash,
                        "timestamp": timestamp_info,
                        "method": "UBI claim",
                        "status": "success",
                        "amount": f"{amount_g:.6f} G$",
                        "activity_type": "ubi_claim"
                    })
        except Exception as e:
            logger.error(f"Error checking UBI Proxy transfers: {e}")

        # --- Check for UBI-specific events on UBI Proxy contract ---
        for event_name, event_signature in UBI_EVENT_SIGNATURES.items():
            if event_name == "TRANSFER":
                continue

            topics = [event_signature]

            if event_name in ["UBI_CLAIMED", "CLAIM", "REWARD_CLAIMED", "UBI_DISTRIBUTED", "DAILY_UBI"]:
                topics.append(_topic_for_address(wallet_address))
            else:
                topics.append(None)

            payload = {
                "jsonrpc": "2.0",
                "method": "eth_getLogs",
                "params": [{
                    "fromBlock": from_block,
                    "toBlock": to_block,
                    "address": ubi_proxy_address,
                    "topics": topics
                }],
                "id": 1
            }

            try:
                session = _get_rpc_session()
                response = session.post(CELO_RPC, json=payload, timeout=15)
                result = response.json()

                if "error" not in result:
                    logs = result.get("result", [])

                    if event_name not in ["UBI_CLAIMED", "CLAIM", "REWARD_CLAIMED", "UBI_DISTRIBUTED", "DAILY_UBI"]:
                        wallet_topic = _topic_for_address(wallet_address)
                        logs = [log for log in logs if wallet_topic in log.get("topics", [])]

                    for log_entry in logs:
                        block_num = int(log_entry.get("blockNumber", "0x0"), 16)
                        tx_hash = log_entry.get("transactionHash", "Unknown")
                        timestamp_info = _format_timestamp(block_num)
                        amount_str = "Event logged"
                        try:
                            data = log_entry.get("data", "0x")
                            if data and data != "0x":
                                amount_g = int(data, 16) / (10 ** 18)
                                amount_str = f"{amount_g:.6f} G$"
                        except Exception:
                            try:
                                log_topics = log_entry.get("topics", [])
                                if len(log_topics) > 2:
                                    amount_g = int(log_topics[2], 16) / (10 ** 18)
                                    amount_str = f"{amount_g:.6f} G$"
                            except Exception:
                                pass

                        all_activities.append({
                            "contract": "UBI Proxy",
                            "contract_address": ubi_proxy_address,
                            "block": block_num,
                            "tx_hash": tx_hash,
                            "timestamp": timestamp_info,
                            "method": event_name.lower().replace("_", " "),
                            "status": "success",
                            "amount": amount_str,
                            "activity_type": "ubi_event"
                        })
            except Exception as e:
                logger.error(f"Error checking {event_name}: {e}")

        if len(all_activities) > 0:
            all_activities.sort(key=lambda x: x["block"], reverse=True)
            latest_activity = all_activities[0]

            claims = [a for a in all_activities if a["activity_type"] == "ubi_claim"]
            events = [a for a in all_activities if a["activity_type"] == "ubi_event"]

            success_message = f"✅ UBI VERIFICATION SUCCESS!\n\n"
            success_message += f"🎯 Found {len(all_activities)} UBI activities from UBI Proxy contract\n"
            success_message += f"   💰 UBI Claims: {len(claims)}\n"
            success_message += f"   📋 Events: {len(events)}\n\n"
            success_message += f"🕐 Most Recent Activity:\n"
            success_message += f"   Contract: {latest_activity['contract']}\n"
            success_message += f"   Type: {latest_activity['method']}\n"
            success_message += f"   Amount: {latest_activity['amount']}\n"
            success_message += f"   Block: #{latest_activity['block']}\n"
            success_message += f"   Time: {latest_activity['timestamp']}\n"
            success_message += f"   Tx: {latest_activity['tx_hash'][:16]}...\n"

            if len(all_activities) > 1:
                success_message += f"\n📊 All UBI Activities (last 24 hours):\n"
                for i, activity in enumerate(all_activities[:5], 1):
                    success_message += f"   {i}. {activity['amount']} ({activity['method']}) - {activity['timestamp']}\n"
                if len(all_activities) > 5:
                    success_message += f"   ... and {len(all_activities) - 5} more activities\n"

            result = {
                "status": "success",
                "message": success_message,
                "activities": all_activities,
                "summary": {
                    "total_activities": len(all_activities),
                    "claims": len(claims),
                    "events": len(events),
                    "contracts_involved": 1,
                    "latest_activity": latest_activity
                }
            }

            _set_ubi_cached(wallet_address, result)
            return result

        else:
            result = {
                "status": "error",
                "message": "You need to claim G$ once every 24 hours to access GoodMarket.\n\nClaim G$ using:\n• MiniPay app (built into Opera Mini)\n• goodwallet.xyz\n• gooddapp.org"
            }
            import time
            with _ubi_cache_lock:
                _ubi_cache[wallet_address.lower()] = {
                    "result": result,
                    "expires_at": time.time() + 120
                }
            return result

    except Exception as e:
        logger.error(f"Exception in UBI verification: {e}")
        return {"status": "error", "message": f"⚠️ UBI verification failed: {e}"}


_GD_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
]


def get_gooddollar_balance(wallet_address: str, include_price: bool = True) -> dict:
    """Fetch on-chain G$ balance for a wallet, with optional USD price enrichment.

    Set ``include_price=False`` to skip the (potentially slow) CoinGecko price
    fetch on the critical path. Callers can fetch the price separately in
    parallel via ``_get_gd_usd_price()`` and merge the result with
    ``enrich_gd_balance_with_price()``.
    """
    import time
    key = wallet_address.lower()
    with _balance_cache_lock:
        entry = _balance_cache.get(key)
        if entry and entry.get("gd") and entry["expires_at"] > time.time():
            logger.debug(f"💰 GD balance cache HIT for {wallet_address[:8]}...")
            cached = entry["gd"]
            cached_has_price = cached.get("gd_usd_price") is not None
            # Serve cached entry unless the caller needs the embedded price
            # but the cached entry was populated without one (e.g. by
            # ``wallet_balances`` which fetches the price separately).
            if (not include_price) or cached_has_price:
                return cached
    try:
        from web3 import Web3
        w3 = _get_w3()
        gooddollar_token = GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"]
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(gooddollar_token),
            abi=_GD_ERC20_ABI
        )
        wallet_checksum = Web3.to_checksum_address(wallet_address)
        balance_wei = contract.functions.balanceOf(wallet_checksum).call()
        balance_g = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_g),
            "balance_formatted": f"{balance_g:,.6f} G$",
            "wallet": wallet_address,
            "contract": gooddollar_token,
        }
        if include_price:
            gd_usd_price = _get_gd_usd_price()
            usd_value = balance_g * gd_usd_price if gd_usd_price > 0 else None
            result["gd_usd_price"] = gd_usd_price
            result["usd_value"] = usd_value if usd_value is not None else 0
            if usd_value is not None:
                result["usd_formatted"] = f"≈ ${usd_value:.4f} USD"
        with _balance_cache_lock:
            existing = _balance_cache.get(key, {})
            _balance_cache[key] = {**existing, "gd": result, "expires_at": time.time() + BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"GD balance check error: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error loading balance"}


def enrich_gd_balance_with_price(gd_result: dict, gd_usd_price: float) -> dict:
    """Return a copy of a GD balance dict with USD price/value fields populated.

    Designed to be paired with ``get_gooddollar_balance(include_price=False)``
    so the price fetch can run in parallel with the balance fetch instead of
    serialised after it.
    """
    if not gd_result or not gd_result.get("success"):
        return gd_result
    enriched = dict(gd_result)
    balance_g = float(enriched.get("balance") or 0)
    usd_value = balance_g * gd_usd_price if gd_usd_price > 0 else None
    enriched["gd_usd_price"] = gd_usd_price
    enriched["usd_value"] = usd_value if usd_value is not None else 0
    if usd_value is not None:
        enriched["usd_formatted"] = f"≈ ${usd_value:.4f} USD"
    return enriched


def is_identity_verified(wallet_address: str) -> dict:
    """Check if a wallet is face-verified on the GoodDollar Identity contract."""
    try:
        from web3 import Web3 as _Web3
        checksum = _Web3.to_checksum_address(wallet_address)
        key = checksum.lower()

        with _identity_cache_lock:
            entry = _identity_cache.get(key)
            if entry and entry["expires_at"] > datetime.now().timestamp():
                logger.debug(f"🪪 Identity cache HIT for {wallet_address[:8]}...")
                return entry["result"]

        w3 = _get_w3()
        contract = w3.eth.contract(
            address=_Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["IDENTITY"]),
            abi=IDENTITY_ABI
        )
        verified = contract.functions.isWhitelisted(checksum).call()
        result = {"verified": verified}

        with _identity_cache_lock:
            _identity_cache[key] = {
                "result": result,
                "expires_at": datetime.now().timestamp() + IDENTITY_CACHE_TTL
            }

        status = "✅ verified" if verified else "❌ not verified"
        logger.info(f"🪪 Identity check for {wallet_address[:8]}...: {status}")
        return result

    except Exception as e:
        logger.error(f"Identity check error for {wallet_address}: {e}")
        return {"verified": False, "error": str(e)}


_fv_expiry_cache: dict = {}
_fv_expiry_cache_lock = threading.Lock()
FV_EXPIRY_CACHE_TTL = 300  # 5 minutes — expiry rarely changes but we want fresh enough countdowns


def get_identity_expiry(wallet_address: str) -> dict:
    """Return face-verification expiry data for a wallet.

    Reads `isWhitelisted`, `dateAuthenticated`, and `authenticationPeriod` from
    the GoodDollar Identity contract. Each call is wrapped independently because
    `dateAuthenticated` / `authenticationPeriod` can revert on some identity
    contract deployments — in that case we still return whitelist status with
    `expiry_available=False` so the UI can show "Verified" without a countdown.
    """
    import time
    from web3 import Web3 as _Web3

    key = (wallet_address or "").lower()

    def _empty(error: str | None = None) -> dict:
        return {
            "success": False,
            "error": error,
            "wallet": key,
            "verified": False,
            "expired": False,
            "ever_verified": False,
            "is_whitelisted": False,
            "expiry_available": False,
            "date_authenticated": 0,
            "date_authenticated_iso": None,
            "authentication_period_days": 0,
            "expires_at": 0,
            "expires_at_iso": None,
            "seconds_remaining": 0,
            "days_remaining": 0,
        }

    try:
        checksum = _Web3.to_checksum_address(wallet_address)
    except Exception as e:
        return _empty(f"invalid_address: {e}")

    with _fv_expiry_cache_lock:
        entry = _fv_expiry_cache.get(key)
        if entry and entry["expires_cache_at"] > time.time():
            return entry["result"]

    try:
        w3 = _get_w3()
        contract = w3.eth.contract(
            address=_Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["IDENTITY"]),
            abi=IDENTITY_ABI
        )
    except Exception as e:
        logger.error(f"FV expiry contract init failed for {wallet_address}: {e}")
        return _empty(str(e))

    # 1) isWhitelisted — required; if this fails, we genuinely can't answer.
    try:
        is_whitelisted = bool(contract.functions.isWhitelisted(checksum).call())
    except Exception as e:
        logger.error(f"FV expiry isWhitelisted failed for {wallet_address}: {e}")
        return _empty(str(e))

    # 2) lastAuthenticated (primary) falling back to dateAuthenticated (older
    #    deployments). On the current Celo Identity contract,
    #    dateAuthenticated() reverts while lastAuthenticated() returns the real
    #    unix timestamp.
    date_auth = 0
    try:
        date_auth = int(contract.functions.lastAuthenticated(checksum).call())
    except Exception as primary_err:
        logger.debug(f"FV expiry lastAuthenticated unavailable for {wallet_address[:8]}…: {primary_err}")
        try:
            date_auth = int(contract.functions.dateAuthenticated(checksum).call())
        except Exception as fv_err:
            logger.debug(f"FV expiry dateAuthenticated also unavailable: {fv_err}")

    # 3) authenticationPeriod — also known to revert on current Celo deployment.
    auth_period_days = 0
    try:
        auth_period_days = int(contract.functions.authenticationPeriod().call())
    except Exception as fv_err:
        logger.debug(f"FV expiry authenticationPeriod unavailable: {fv_err}")

    now = int(time.time())
    ever_verified = date_auth > 0
    expiry_available = ever_verified and auth_period_days > 0
    expires_at = (date_auth + auth_period_days * 86400) if expiry_available else 0
    seconds_remaining = max(0, expires_at - now) if expires_at > 0 else 0
    days_remaining = seconds_remaining // 86400 if seconds_remaining > 0 else 0
    expired = expiry_available and now > expires_at

    # Without reliable expiry data we trust on-chain whitelist status.
    if expiry_available:
        effective_verified = is_whitelisted and not expired
    else:
        effective_verified = is_whitelisted

    result = {
        "success": True,
        "wallet": key,
        "is_whitelisted": is_whitelisted,
        "ever_verified": ever_verified,
        "verified": effective_verified,
        "expired": bool(expired),
        "expiry_available": expiry_available,
        "date_authenticated": date_auth,
        "date_authenticated_iso": (
            datetime.fromtimestamp(date_auth, tz=timezone.utc).isoformat()
            if ever_verified else None
        ),
        "authentication_period_days": auth_period_days,
        "expires_at": expires_at,
        "expires_at_iso": (
            datetime.fromtimestamp(expires_at, tz=timezone.utc).isoformat()
            if expires_at > 0 else None
        ),
        "seconds_remaining": int(seconds_remaining),
        "days_remaining": int(days_remaining),
    }

    with _fv_expiry_cache_lock:
        _fv_expiry_cache[key] = {
            "result": result,
            "expires_cache_at": time.time() + FV_EXPIRY_CACHE_TTL,
        }

    logger.info(
        f"🪪 FV expiry for {wallet_address[:8]}…: "
        f"whitelisted={is_whitelisted}, expiry_available={expiry_available}, "
        f"expired={expired}, days_remaining={days_remaining}"
    )
    return result


def invalidate_fv_expiry_cache(wallet_address: str | None = None) -> None:
    """Drop cached FV expiry data (for a wallet, or all if None)."""
    with _fv_expiry_cache_lock:
        if wallet_address:
            _fv_expiry_cache.pop(wallet_address.lower(), None)
        else:
            _fv_expiry_cache.clear()


_entitlement_cache: dict = {}
_entitlement_cache_lock = threading.Lock()
ENTITLEMENT_CACHE_TTL = 180  # 3 minutes — short enough to reflect claim/status changes

def get_ubi_claim_calldata() -> str:
    """Return encoded calldata for UBIScheme claim()."""
    return "0x4e71d92d"


def get_ubi_entitlement(wallet_address: str) -> dict:
    """Check how much G$ the wallet can claim right now from the UBIScheme contract."""
    import time
    key = wallet_address.lower()
    with _entitlement_cache_lock:
        entry = _entitlement_cache.get(key)
        if entry and entry["expires_at"] > time.time():
            logger.debug(f"⚡ UBI entitlement cache HIT for {wallet_address[:8]}...")
            return entry["result"]

    try:
        from web3 import Web3
        w3 = _get_w3()
        wallet_checksum = Web3.to_checksum_address(wallet_address)

        identity_contract = w3.eth.contract(
            address=Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["IDENTITY"]),
            abi=IDENTITY_ABI
        )
        is_verified = identity_contract.functions.isWhitelisted(wallet_checksum).call()

        # Compute entitlement even for unverified users so the UI can show the
        # pending claim amount before Face Verification is completed.
        entitlement_wei = 0
        entitlement_g = 0.0
        try:
            ubi_contract = w3.eth.contract(
                address=Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["UBI_PROXY"]),
                abi=UBI_SCHEME_ABI
            )
            entitlement_wei = ubi_contract.functions.checkEntitlement(wallet_checksum).call()
            entitlement_g = entitlement_wei / (10 ** 18)
        except Exception as entitlement_err:
            logger.warning(
                f"⚠️ Could not fetch entitlement for {wallet_address[:8]}... before FV check: {entitlement_err}"
            )

        if not is_verified:
            result = {
                "success": True,
                "wallet": key,
                "is_verified": False,
                "can_claim": False,
                "entitlement": float(entitlement_g),
                "entitlement_formatted": f"{entitlement_g:.2f}",
                "reason": "not_verified"
            }
            with _entitlement_cache_lock:
                _entitlement_cache[key] = {"result": result, "expires_at": time.time() + ENTITLEMENT_CACHE_TTL}
            return result

        # Whitelisted — check if Face Verification has lapsed (still in whitelist but auth expired).
        # On the current Celo Identity contract `dateAuthenticated` reverts, so we try
        # `lastAuthenticated` first (the working function) and fall back to `dateAuthenticated`
        # for older deployments. `authenticationPeriod` is also wrapped separately.
        fv_lapsed = False
        date_auth = 0
        try:
            date_auth = identity_contract.functions.lastAuthenticated(wallet_checksum).call()
        except Exception:
            try:
                date_auth = identity_contract.functions.dateAuthenticated(wallet_checksum).call()
            except Exception as fv_err:
                logger.debug(f"FV lapse: no auth-date fn available for {wallet_address[:8]}…: {fv_err}")

        auth_period = 0
        try:
            auth_period = identity_contract.functions.authenticationPeriod().call()
        except Exception as ap_err:
            logger.debug(f"FV lapse: authenticationPeriod unavailable: {ap_err}")

        if date_auth > 0 and auth_period > 0:
            auth_expires_at = date_auth + auth_period * 86400
            if time.time() > auth_expires_at:
                fv_lapsed = True

        if fv_lapsed:
            result = {
                "success": True,
                "wallet": key,
                "is_verified": False,
                "can_claim": False,
                "entitlement": float(entitlement_g),
                "entitlement_formatted": f"{entitlement_g:.2f}",
                "reason": "re_verification_needed"
            }
            with _entitlement_cache_lock:
                _entitlement_cache[key] = {"result": result, "expires_at": time.time() + ENTITLEMENT_CACHE_TTL}
            return result

        result = {
            "success": True,
            "wallet": key,
            "is_verified": True,
            "entitlement": float(entitlement_g),
            "entitlement_formatted": f"{entitlement_g:.2f}",
            "can_claim": entitlement_g > 0,
            "claim_calldata": get_ubi_claim_calldata(),
            "ubi_contract": GOODDOLLAR_CONTRACTS["UBI_PROXY"]
        }
        with _entitlement_cache_lock:
            _entitlement_cache[key] = {"result": result, "expires_at": time.time() + ENTITLEMENT_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"UBI entitlement check error for {wallet_address[:8]}...: {e}")
        return {"success": False, "error": str(e), "entitlement": 0, "can_claim": False, "is_verified": None}


def invalidate_entitlement_cache(wallet_address: str):
    """Call this after a successful UBI claim to force fresh data on next check."""
    key = wallet_address.lower()
    with _entitlement_cache_lock:
        _entitlement_cache.pop(key, None)


def get_celo_balance(wallet_address: str) -> dict:
    """Get native CELO balance for a wallet address (cached 2 minutes)."""
    import time
    key = wallet_address.lower()
    with _balance_cache_lock:
        entry = _balance_cache.get(key)
        if entry and entry.get("celo") and entry["expires_at"] > time.time():
            logger.debug(f"⚡ CELO balance cache HIT for {wallet_address[:8]}...")
            return entry["celo"]
    try:
        from web3 import Web3
        w3 = _get_w3()
        checksum = Web3.to_checksum_address(wallet_address)
        balance_wei = w3.eth.get_balance(checksum)
        balance_celo = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_celo),
            "balance_wei": str(balance_wei),
            "balance_formatted": f"{balance_celo:.6f} CELO",
            "wallet": wallet_address,
        }
        with _balance_cache_lock:
            existing = _balance_cache.get(key, {})
            _balance_cache[key] = {**existing, "celo": result, "expires_at": time.time() + BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"CELO balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def get_cusd_balance(wallet_address: str) -> dict:
    """Get cUSD (Celo Dollar) ERC-20 balance for a wallet address (cached 2 minutes)."""
    import time
    key = wallet_address.lower()
    with _balance_cache_lock:
        entry = _balance_cache.get(key)
        if entry and entry.get("cusd") and entry["expires_at"] > time.time():
            logger.debug(f"💵 cUSD balance cache HIT for {wallet_address[:8]}...")
            return entry["cusd"]
    try:
        from web3 import Web3
        w3 = _get_w3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(CUSD_CONTRACT),
            abi=_GD_ERC20_ABI
        )
        wallet_checksum = Web3.to_checksum_address(wallet_address)
        balance_wei = contract.functions.balanceOf(wallet_checksum).call()
        balance_cusd = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_cusd),
            "balance_formatted": f"{balance_cusd:.6f} cUSD",
            "wallet": wallet_address,
            "contract": CUSD_CONTRACT,
        }
        with _balance_cache_lock:
            existing = _balance_cache.get(key, {})
            _balance_cache[key] = {**existing, "cusd": result, "expires_at": time.time() + BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"cUSD balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def get_usdt_balance(wallet_address: str) -> dict:
    """Get USDT (Tether on Celo) ERC-20 balance for a wallet address (cached 2 minutes)."""
    import time
    key = wallet_address.lower()
    with _balance_cache_lock:
        entry = _balance_cache.get(key)
        if entry and entry.get("usdt") and entry["expires_at"] > time.time():
            logger.debug(f"💵 USDT balance cache HIT for {wallet_address[:8]}...")
            return entry["usdt"]
    try:
        from web3 import Web3
        w3 = _get_w3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(USDT_CONTRACT),
            abi=_GD_ERC20_ABI
        )
        wallet_checksum = Web3.to_checksum_address(wallet_address)
        balance_raw = contract.functions.balanceOf(wallet_checksum).call()
        balance_usdt = balance_raw / (10 ** 6)
        result = {
            "success": True,
            "balance": float(balance_usdt),
            "balance_formatted": f"{balance_usdt:.6f} USDT",
            "wallet": wallet_address,
            "contract": USDT_CONTRACT,
        }
        with _balance_cache_lock:
            existing = _balance_cache.get(key, {})
            _balance_cache[key] = {**existing, "usdt": result, "expires_at": time.time() + BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"USDT balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def prepare_usdt_transfer_data(to_address: str, amount_usdt: float) -> dict:
    """
    Return the ABI-encoded calldata for a USDT ERC-20 transfer(address,uint256) call.
    USDT on Celo uses 6 decimals.
    """
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        token_addr = Web3.to_checksum_address(USDT_CONTRACT)
        to_checksum = Web3.to_checksum_address(to_address)
        amount_raw = int(amount_usdt * (10 ** 6))
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_raw])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": token_addr,
            "data": data,
            "value": "0x0",
            "chain_id": CELO_CHAIN_ID,
            "token": "USDT",
            "recipient": to_checksum,
            "amount": amount_usdt,
        }
    except Exception as e:
        logger.error(f"prepare_usdt_transfer_data error: {e}")
        return {"success": False, "error": str(e)}


def prepare_cusd_transfer_data(to_address: str, amount_cusd: float) -> dict:
    """
    Return the ABI-encoded calldata for a cUSD ERC-20 transfer(address,uint256) call.
    """
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        token_addr = Web3.to_checksum_address(CUSD_CONTRACT)
        to_checksum = Web3.to_checksum_address(to_address)
        amount_wei = int(amount_cusd * (10 ** 18))
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_wei])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": token_addr,
            "data": data,
            "value": "0x0",
            "chain_id": CELO_CHAIN_ID,
            "token": "cUSD",
            "recipient": to_checksum,
            "amount": amount_cusd,
        }
    except Exception as e:
        logger.error(f"prepare_cusd_transfer_data error: {e}")
        return {"success": False, "error": str(e)}


def get_wallet_transfer_history(wallet_address: str, limit: int = 30) -> list:
    """
    Get G$ (ERC-20) transfer events involving this wallet (both sent and received).
    Returns a list of transfer dicts sorted newest-first. Cached for 5 minutes.
    """
    import time
    key = wallet_address.lower()
    with _transfer_history_cache_lock:
        entry = _transfer_history_cache.get(key)
        if entry and entry["expires_at"] > time.time():
            logger.debug(f"📜 Transfer history cache HIT for {wallet_address[:8]}...")
            return entry["transfers"][:limit]
    try:
        gooddollar_token = GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"]
        search_hours = 24 * 7  # 7 days instead of 30 — much faster
        from_block_hex, to_block_hex = _calculate_block_range(search_hours)
        from_block_int = int(from_block_hex, 16)
        to_block_int = int(to_block_hex, 16)

        wallet_topic = _topic_for_address(wallet_address)
        TRANSFER_SIG = UBI_EVENT_SIGNATURES["TRANSFER"]
        all_logs = []

        params_sent = {
            "address": gooddollar_token,
            "topics": [TRANSFER_SIG, wallet_topic, None],
        }
        params_received = {
            "address": gooddollar_token,
            "topics": [TRANSFER_SIG, None, wallet_topic],
        }

        for params in [params_sent, params_received]:
            logs = _get_logs_chunked(params, from_block_int, to_block_int, chunk_size=2000)
            all_logs.extend(logs)

        transfers = []
        wallet_lower = wallet_address.lower()

        for log_entry in all_logs:
            topics = log_entry.get("topics", [])
            if len(topics) < 3:
                continue
            from_addr = "0x" + topics[1][-40:]
            to_addr = "0x" + topics[2][-40:]
            direction = "sent" if from_addr.lower() == wallet_lower else "received"
            block_num = int(log_entry.get("blockNumber", "0x0"), 16)
            tx_hash = log_entry.get("transactionHash", "")
            try:
                amount_g = int(log_entry.get("data", "0x0"), 16) / (10 ** 18)
            except Exception:
                amount_g = 0
            transfers.append({
                "token": "G$",
                "direction": direction,
                "from": from_addr,
                "to": to_addr,
                "amount": float(amount_g),
                "amount_formatted": f"{amount_g:.4f} G$",
                "block": block_num,
                "tx_hash": tx_hash,
                "timestamp": _format_timestamp(block_num),
                "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash}",
            })

        transfers.sort(key=lambda x: x["block"], reverse=True)
        import time
        with _transfer_history_cache_lock:
            _transfer_history_cache[key] = {
                "transfers": transfers,
                "expires_at": time.time() + TRANSFER_HISTORY_CACHE_TTL,
            }
        return transfers[:limit]

    except Exception as e:
        logger.error(f"Transfer history error for {wallet_address}: {e}")
        return []


# Known contract addresses used for transaction classification
_UNISWAP_ROUTER_CELO = "0x5615cdab10dc425a742d643d949a7f474c01abc4"

# Separate cache for comprehensive multi-token tx history
_tx_history_cache: dict = {}
_tx_history_cache_lock = threading.Lock()
TX_HISTORY_CACHE_TTL = 300  # 5 minutes


def get_comprehensive_tx_history(wallet_address: str, limit: int = 50, force: bool = False) -> list:
    """
    Return classified transaction history across G$, cUSD, and USDT for the last 14 days.

    Each item has:
      tx_type  – 'claim' | 'swap' | 'savings_deposit' | 'savings_withdraw' |
                 'transfer_sent' | 'transfer_received'
      label    – human-readable description
      token    – 'G$' | 'cUSD' | 'USDT'

    Key improvements over the old single-token version:
    • Fetches G$, cUSD, USDT ERC-20 transfer events (all sends & receives)
    • Uses _get_logs_full so the ENTIRE date window is scanned, not just the
      first block-chunk that happens to have activity
    • Swap detection: batch-fetches transaction details in a single round-trip
      and checks tx.to == Uniswap Router (pools emit transfers, not the router)
    • Separate 5-minute cache to avoid re-scanning on every tab switch
    """
    import os as _os, time as _time

    cache_key = wallet_address.lower()
    if not force:
        with _tx_history_cache_lock:
            entry = _tx_history_cache.get(cache_key)
            if entry and entry["expires_at"] > _time.time():
                logger.debug(f"[tx-history] cache HIT for {wallet_address[:8]}…")
                return entry["txs"][:limit]
    else:
        logger.info(f"[tx-history] force refresh for {wallet_address[:8]}…")

    savings_addr = _os.getenv("SAVINGS_CONTRACT_ADDRESS", "").lower()
    ubi_proxy    = GOODDOLLAR_CONTRACTS["UBI_PROXY"].lower()

    TOKENS = [
        {"symbol": "G$",   "address": GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"], "decimals": 18},
        {"symbol": "cUSD", "address": CUSD_CONTRACT,  "decimals": 18},
        {"symbol": "USDT", "address": USDT_CONTRACT,  "decimals": 6},
    ]
    TRANSFER_SIG = UBI_EVENT_SIGNATURES["TRANSFER"]

    # 14-day window; 720 blocks/hour on Celo
    search_hours   = 24 * 14
    from_block_hex, to_block_hex = _calculate_block_range(search_hours)
    from_block_int = int(from_block_hex, 16)
    to_block_int   = int(to_block_hex,   16)

    wallet_topic = _topic_for_address(wallet_address)
    wallet_lower = wallet_address.lower()

    now_ts = _time.time()
    raw_transfers = []

    for token in TOKENS:
        # Fetch sent events (wallet is the `from`)
        sent_params = {
            "address": token["address"],
            "topics": [TRANSFER_SIG, wallet_topic, None],
        }
        # Fetch received events (wallet is the `to`)
        recv_params = {
            "address": token["address"],
            "topics": [TRANSFER_SIG, None, wallet_topic],
        }

        for params in [sent_params, recv_params]:
            logs = _get_logs_full(params, from_block_int, to_block_int, chunk_size=10000)
            for log_entry in logs:
                tlist = log_entry.get("topics", [])
                if len(tlist) < 3:
                    continue
                from_addr = "0x" + tlist[1][-40:]
                to_addr   = "0x" + tlist[2][-40:]
                block_num = int(log_entry.get("blockNumber", "0x0"), 16)
                tx_hash   = log_entry.get("transactionHash", "")
                try:
                    amount = int(log_entry.get("data", "0x0"), 16) / (10 ** token["decimals"])
                except Exception:
                    amount = 0

                direction = "sent" if from_addr.lower() == wallet_lower else "received"
                # Format nicely: 2 decimals for cUSD/USDT, 4 for G$
                decimals_display = 2 if token["symbol"] in ("cUSD", "USDT") else 4
                # Approximate Unix timestamp from block number (Celo ~5 sec/block)
                approx_ts = now_ts - (to_block_int - block_num) * 5
                raw_transfers.append({
                    "network":          "celo",
                    "token":            token["symbol"],
                    "direction":        direction,
                    "from":             from_addr,
                    "to":               to_addr,
                    "amount":           float(amount),
                    "amount_formatted": f"{amount:.{decimals_display}f} {token['symbol']}",
                    "block":            block_num,
                    "tx_hash":          tx_hash,
                    "timestamp":        _format_timestamp(block_num),
                    "explorer_url":     f"https://celoscan.io/tx/{tx_hash}",
                    "_sort_ts":         approx_ts,
                })

    # Deduplicate — same event can appear in both sent and received queries
    seen: set = set()
    unique_transfers = []
    for tx in raw_transfers:
        key = (tx["tx_hash"], tx["token"], tx["from"].lower(), tx["to"].lower())
        if key not in seen:
            seen.add(key)
            unique_transfers.append(tx)

    # Sort newest-first by approximate Unix timestamp
    unique_transfers.sort(key=lambda x: x.get("_sort_ts", 0), reverse=True)
    # Trim before expensive swap-detection batch call (Celo only)
    unique_transfers = unique_transfers[:min(limit * 3, 150)]

    # ── Batch-fetch tx details to detect Uniswap Router calls (swaps) ────────
    # Only Celo transactions need this check; XDC G$ txs are pre-classified.
    celo_hashes = list({tx["tx_hash"] for tx in unique_transfers
                        if tx.get("tx_hash") and tx.get("network") == "celo"})
    swap_hashes: set = set()

    if celo_hashes:
        batch_payload = [
            {"jsonrpc": "2.0", "method": "eth_getTransactionByHash",
             "params": [h], "id": idx}
            for idx, h in enumerate(celo_hashes)
        ]
        try:
            session = _get_rpc_session()
            resp = session.post(CELO_RPC, json=batch_payload, timeout=25)
            batch_results = resp.json()
            if isinstance(batch_results, list):
                for item in batch_results:
                    tx_data = item.get("result") or {}
                    if (tx_data.get("to") or "").lower() == _UNISWAP_ROUTER_CELO:
                        h = (tx_data.get("hash") or "").lower()
                        if h:
                            swap_hashes.add(h)
        except Exception as exc:
            logger.warning(f"[tx-history] batch tx-hash fetch failed: {exc}")
    # ─────────────────────────────────────────────────────────────────────────

    # Classify each Celo transfer; XDC transfers already have tx_type set
    celo_classified = []
    for tx in unique_transfers:
        if tx.get("network") == "xdc":
            # Already classified by get_xdc_gd_transfer_history; just keep it
            celo_classified.append(tx)
            continue

        from_addr     = (tx.get("from")    or "").lower()
        to_addr       = (tx.get("to")      or "").lower()
        tx_hash_lower = (tx.get("tx_hash") or "").lower()
        token         = tx.get("token", "G$")

        if tx_hash_lower in swap_hashes:
            tx_type = "swap"
            label   = f"Token Swap ({token})"
        elif from_addr == ubi_proxy:
            tx_type = "claim"
            label   = "G$ UBI Claim"
        elif savings_addr and to_addr == savings_addr:
            tx_type = "savings_deposit"
            label   = "Savings Deposit"
        elif savings_addr and from_addr == savings_addr:
            tx_type = "savings_withdraw"
            label   = "Savings Withdrawal"
        elif tx.get("direction") == "sent":
            tx_type = "transfer_sent"
            label   = f"Sent {token}"
        else:
            tx_type = "transfer_received"
            label   = f"Received {token}"

        celo_classified.append({**tx, "tx_type": tx_type, "label": label})

    # ── Merge in XDC G$ transfer history ─────────────────────────────────────
    try:
        xdc_txs = get_xdc_gd_transfer_history(wallet_address, limit=50)
        # _sort_ts is already set as approx Unix ts by get_xdc_gd_transfer_history;
        # fall back to 0 only if missing (shouldn't happen)
        for xtx in xdc_txs:
            if "_sort_ts" not in xtx:
                xtx["_sort_ts"] = 0.0
        celo_classified.extend(xdc_txs)
    except Exception as xdc_err:
        logger.warning(f"[tx-history] XDC G$ fetch failed (non-fatal): {xdc_err}")
    # ─────────────────────────────────────────────────────────────────────────

    # Re-sort after merging and strip internal sort key
    celo_classified.sort(key=lambda x: x.get("_sort_ts", 0), reverse=True)
    result = []
    for tx in celo_classified:
        clean = {k: v for k, v in tx.items() if k != "_sort_ts"}
        result.append(clean)

    # Cache and return
    with _tx_history_cache_lock:
        _tx_history_cache[cache_key] = {
            "txs":        result,
            "expires_at": _time.time() + TX_HISTORY_CACHE_TTL,
        }

    return result[:limit]


def prepare_gd_transfer_data(to_address: str, amount_gd: float) -> dict:
    """
    Return the ABI-encoded calldata for an ERC-20 transfer(address,uint256) call.
    This is sent to the frontend so the user can sign it with their own wallet.
    """
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        token_addr = Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["GOODDOLLAR_TOKEN"])
        to_checksum = Web3.to_checksum_address(to_address)
        amount_wei = int(amount_gd * (10 ** 18))
        # Encode transfer(address,uint256) calldata manually — works on all web3.py versions
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_wei])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": token_addr,
            "data": data,
            "value": "0x0",
            "chain_id": CELO_CHAIN_ID,
            "token": "G$",
            "recipient": to_checksum,
            "amount": amount_gd,
        }
    except Exception as e:
        logger.error(f"prepare_gd_transfer_data error: {e}")
        return {"success": False, "error": str(e)}


def check_ubi_entitlement(wallet_address: str) -> dict:
    """
    Check how much G$ a wallet can claim right now from the UBI pool.
    Returns claimable amount in G$ (human-readable) and raw wei value.
    Also returns the encoded claim() calldata so the frontend can send the tx.
    """
    try:
        w3 = _get_w3()
        checksum_wallet = w3.to_checksum_address(wallet_address)
        ubi_proxy = w3.eth.contract(
            address=w3.to_checksum_address(GOODDOLLAR_CONTRACTS["UBI_PROXY"]),
            abi=UBI_SCHEME_ABI
        )

        entitlement_wei = ubi_proxy.functions.checkEntitlement(checksum_wallet).call()
        entitlement_gd = entitlement_wei / (10 ** 18)

        # Encode claim() calldata: keccak256("claim()")[0:4] = 0x4e71d92d
        claim_selector = w3.keccak(text="claim()")[:4].hex()
        claim_calldata = "0x" + claim_selector

        whitelisted = is_identity_verified(wallet_address)

        return {
            "success": True,
            "wallet": wallet_address,
            "claimable_wei": str(entitlement_wei),
            "claimable_gd": round(entitlement_gd, 4),
            "can_claim": entitlement_wei > 0,
            "whitelisted": whitelisted,
            "ubi_contract": GOODDOLLAR_CONTRACTS["UBI_PROXY"],
            "claim_calldata": claim_calldata,
            "chain_id": CELO_CHAIN_ID,
        }
    except Exception as e:
        logger.error(f"check_ubi_entitlement error for {wallet_address}: {e}")
        return {
            "success": False,
            "error": str(e),
            "can_claim": False,
            "claimable_gd": 0,
        }



# ============================
# XDC Network Integration
# ============================

XDC_CHAIN_ID = int(os.getenv("XDC_CHAIN_ID", "50"))
XDC_RPC = os.getenv("XDC_RPC_URL", "https://erpc.xinfin.network")

# Common XDC-ecosystem tokens (mainnet)
XUSDT_CONTRACT = os.getenv("XUSDT_CONTRACT", "0xD4B5f10D61916Bd6E0860144a91Ac658dE8a1437")
SRX_CONTRACT   = os.getenv("SRX_CONTRACT",   "0x17B217490e1c17dD9d41E5c8f3fF7DE1bd38eE60")

# GoodDollar contracts on XDC Network (production-xdc)
XDC_GD_TOKEN    = os.getenv("XDC_GD_TOKEN",   "0xEC2136843a983885AebF2feB3931F73A8eBEe50c")
XDC_UBI_SCHEME  = os.getenv("XDC_UBI_SCHEME", "0x22867567E2D80f2049200E25C6F31CB6Ec2F0faf")
XDC_IDENTITY    = os.getenv("XDC_IDENTITY",   "0x27a4a02C9ed591E1a86e2e5D05870292c34622C9")
XDC_GD_DECIMALS = 18  # G$ on XDC uses 18 decimal places.

# GoodDollar on Fuse mainnet. Defaults come from GoodDollar docs; the decimals
# value is still read from-chain when possible so deployments can override safely.
FUSE_CHAIN_ID = int(os.getenv("FUSE_CHAIN_ID", "122"))
FUSE_RPC = os.getenv("FUSE_RPC_URL", "https://rpc.fuse.io")
FUSE_GD_TOKEN = os.getenv("FUSE_GD_TOKEN", "0x495d133B938596C9984d462F007B676bDc57eCEC")
FUSE_GD_DECIMALS = int(os.getenv("FUSE_GD_DECIMALS", "2"))
FUSE_UBI_SCHEME = os.getenv("FUSE_UBI_SCHEME", "0xd253A5203817225e9768C05E5996d642fb96bA86")

_xdc_w3_singleton = None
_xdc_w3_lock = threading.Lock()
_fuse_w3_singleton = None
_fuse_w3_lock = threading.Lock()

_xdc_balance_cache: dict = {}
_xdc_balance_cache_lock = threading.Lock()
XDC_BALANCE_CACHE_TTL = 120  # 2 minutes

_fuse_balance_cache: dict = {}
_fuse_balance_cache_lock = threading.Lock()
FUSE_BALANCE_CACHE_TTL = 120  # 2 minutes


def _get_xdc_w3():
    global _xdc_w3_singleton
    with _xdc_w3_lock:
        if _xdc_w3_singleton is None or not _xdc_w3_singleton.is_connected():
            from web3 import Web3 as _W3
            _xdc_w3_singleton = _W3(_W3.HTTPProvider(XDC_RPC, request_kwargs={"timeout": 10}))
        return _xdc_w3_singleton


def _get_fuse_w3():
    global _fuse_w3_singleton
    with _fuse_w3_lock:
        if _fuse_w3_singleton is None or not _fuse_w3_singleton.is_connected():
            from web3 import Web3 as _W3
            _fuse_w3_singleton = _W3(_W3.HTTPProvider(FUSE_RPC, request_kwargs={"timeout": 10}))
        return _fuse_w3_singleton


def _normalize_xdc_address(address: str) -> str:
    """Convert xdc... prefix to 0x... so web3.py can handle it."""
    if address.lower().startswith("xdc"):
        return "0x" + address[3:]
    return address


def get_xdc_balance(wallet_address: str) -> dict:
    """Get native XDC balance for a wallet address (cached 2 minutes)."""
    import time
    norm = _normalize_xdc_address(wallet_address)
    key = norm.lower()
    with _xdc_balance_cache_lock:
        entry = _xdc_balance_cache.get(key)
        if entry and entry.get("xdc") and entry["expires_at"] > time.time():
            return entry["xdc"]
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        checksum = Web3.to_checksum_address(norm)
        balance_wei = w3.eth.get_balance(checksum)
        balance_xdc = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_xdc),
            "balance_wei": str(balance_wei),
            "balance_formatted": f"{balance_xdc:.6f} XDC",
            "wallet": wallet_address,
        }
        with _xdc_balance_cache_lock:
            existing = _xdc_balance_cache.get(key, {})
            _xdc_balance_cache[key] = {**existing, "xdc": result, "expires_at": time.time() + XDC_BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"XDC balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


_XDC_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "_owner", "type": "address"}],
     "name": "balanceOf", "outputs": [{"name": "balance", "type": "uint256"}], "type": "function"},
    {"constant": True, "inputs": [], "name": "decimals",
     "outputs": [{"name": "", "type": "uint8"}], "type": "function"},
]


def _get_xdc_token_balance(wallet_address: str, token_contract: str, cache_key: str) -> dict:
    """Generic XDC ERC-20 balance fetch with caching."""
    import time
    norm = _normalize_xdc_address(wallet_address)
    key = norm.lower()
    with _xdc_balance_cache_lock:
        entry = _xdc_balance_cache.get(key)
        if entry and entry.get(cache_key) and entry["expires_at"] > time.time():
            return entry[cache_key]
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        contract = w3.eth.contract(
            address=Web3.to_checksum_address(token_contract),
            abi=_XDC_ERC20_ABI
        )
        checksum = Web3.to_checksum_address(norm)
        balance_raw = contract.functions.balanceOf(checksum).call()
        try:
            decimals = contract.functions.decimals().call()
        except Exception:
            decimals = 6
        balance = balance_raw / (10 ** decimals)
        result = {
            "success": True,
            "balance": float(balance),
            "balance_formatted": f"{balance:.6f}",
            "wallet": wallet_address,
            "contract": token_contract,
        }
        with _xdc_balance_cache_lock:
            existing = _xdc_balance_cache.get(key, {})
            _xdc_balance_cache[key] = {**existing, cache_key: result, "expires_at": time.time() + XDC_BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"XDC token {cache_key} balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def get_xusdt_balance(wallet_address: str) -> dict:
    return _get_xdc_token_balance(wallet_address, XUSDT_CONTRACT, "xusdt")


def get_fuse_balance(wallet_address: str) -> dict:
    """Get native FUSE balance for a wallet address (cached 2 minutes)."""
    import time
    key = wallet_address.lower()
    with _fuse_balance_cache_lock:
        entry = _fuse_balance_cache.get(key)
        if entry and entry.get("fuse") and entry["expires_at"] > time.time():
            return entry["fuse"]
    try:
        from web3 import Web3
        w3 = _get_fuse_w3()
        checksum = Web3.to_checksum_address(wallet_address)
        balance_wei = w3.eth.get_balance(checksum)
        balance_fuse = balance_wei / (10 ** 18)
        result = {
            "success": True,
            "balance": float(balance_fuse),
            "balance_wei": str(balance_wei),
            "balance_formatted": f"{balance_fuse:.6f} FUSE",
            "wallet": wallet_address,
            "network": "fuse",
        }
        with _fuse_balance_cache_lock:
            existing = _fuse_balance_cache.get(key, {})
            _fuse_balance_cache[key] = {**existing, "fuse": result, "expires_at": time.time() + FUSE_BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"FUSE balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0, "balance_formatted": "Error"}


def _get_fuse_token_decimals(token_contract: str) -> int:
    try:
        from web3 import Web3
        w3 = _get_fuse_w3()
        contract = w3.eth.contract(address=Web3.to_checksum_address(token_contract), abi=_XDC_ERC20_ABI)
        return int(contract.functions.decimals().call())
    except Exception:
        return FUSE_GD_DECIMALS


def get_fuse_gd_balance(wallet_address: str) -> dict:
    """Get G$ (GoodDollar) balance on Fuse Network."""
    import time
    key = wallet_address.lower()
    with _fuse_balance_cache_lock:
        entry = _fuse_balance_cache.get(key)
        if entry and entry.get("gd") and entry["expires_at"] > time.time():
            return entry["gd"]
    try:
        from web3 import Web3
        w3 = _get_fuse_w3()
        checksum = Web3.to_checksum_address(wallet_address)
        token = Web3.to_checksum_address(FUSE_GD_TOKEN)
        contract = w3.eth.contract(address=token, abi=_XDC_ERC20_ABI)
        balance_raw = contract.functions.balanceOf(checksum).call()
        decimals = _get_fuse_token_decimals(FUSE_GD_TOKEN)
        balance = balance_raw / (10 ** decimals)
        result = {
            "success": True,
            "balance": float(balance),
            "balance_raw": str(balance_raw),
            "decimals": decimals,
            "token": "G$",
            "network": "fuse",
            "wallet": wallet_address,
            "contract": FUSE_GD_TOKEN,
        }
        with _fuse_balance_cache_lock:
            existing = _fuse_balance_cache.get(key, {})
            _fuse_balance_cache[key] = {**existing, "gd": result, "expires_at": time.time() + FUSE_BALANCE_CACHE_TTL}
        return result
    except Exception as e:
        logger.error(f"get_fuse_gd_balance error for {wallet_address}: {e}")
        return {"success": False, "error": str(e), "balance": 0.0}


def check_fuse_ubi_entitlement(wallet_address: str) -> dict:
    """Check how much G$ the wallet can claim on Fuse Network via UBIScheme.checkEntitlement()."""
    try:
        from web3 import Web3
        w3 = _get_fuse_w3()
        checksum = Web3.to_checksum_address(wallet_address)
        ubi_addr = Web3.to_checksum_address(FUSE_UBI_SCHEME)
        abi = [
            {"inputs":[],"name":"checkEntitlement","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
            {"inputs":[{"name":"_account","type":"address"}],"name":"checkEntitlement","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
        ]
        contract = w3.eth.contract(address=ubi_addr, abi=abi)
        try:
            raw = contract.functions.checkEntitlement(checksum).call()
        except Exception:
            raw = contract.functions.checkEntitlement().call({'from': checksum})
        claimable = raw / (10 ** FUSE_GD_DECIMALS)
        return {
            "success": True,
            "claimable": float(claimable),
            "claimable_raw": str(raw),
            "can_claim": claimable > 0,
            "network": "fuse",
            "ubi_contract": FUSE_UBI_SCHEME,
            "chain_id": FUSE_CHAIN_ID,
        }
    except Exception as e:
        logger.error(f"check_fuse_ubi_entitlement error: {e}")
        return {"success": False, "error": str(e), "claimable": 0.0, "can_claim": False, "network": "fuse"}


def prepare_fuse_gd_send_data(to_address: str, amount: float) -> dict:
    """Prepare Fuse G$ ERC-20 transfer calldata."""
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        to_checksum = Web3.to_checksum_address(to_address)
        token = Web3.to_checksum_address(FUSE_GD_TOKEN)
        decimals = _get_fuse_token_decimals(FUSE_GD_TOKEN)
        amount_raw = int(amount * (10 ** decimals))
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_raw])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": token,
            "data": data,
            "value": "0x0",
            "chain_id": FUSE_CHAIN_ID,
            "token": "FUSE_GD",
            "recipient": to_checksum,
            "amount": amount,
            "decimals": decimals,
        }
    except Exception as e:
        logger.error(f"prepare_fuse_gd_send_data error: {e}")
        return {"success": False, "error": str(e)}


def prepare_fuse_send_data(to_address: str, amount_fuse: float) -> dict:
    """Prepare native FUSE send transaction parameters."""
    try:
        from web3 import Web3
        to_checksum = Web3.to_checksum_address(to_address)
        amount_wei = int(amount_fuse * (10 ** 18))
        return {
            "success": True,
            "to": to_checksum,
            "data": "0x",
            "value": hex(amount_wei),
            "chain_id": FUSE_CHAIN_ID,
            "token": "FUSE",
            "recipient": to_checksum,
            "amount": amount_fuse,
        }
    except Exception as e:
        logger.error(f"prepare_fuse_send_data error: {e}")
        return {"success": False, "error": str(e)}


def get_xdc_transfer_history(wallet_address: str, limit: int = 30) -> list:
    """
    Fetch recent XDC transfer events using XDCScan API (block explorer).
    Falls back to empty list if the API is unavailable.
    """
    import requests as _req
    norm = _normalize_xdc_address(wallet_address)
    try:
        api_url = "https://xdc.blocksscan.io/api"
        params = {
            "module": "account",
            "action": "txlist",
            "address": norm,
            "sort": "desc",
            "limit": limit,
        }
        resp = _req.get(api_url, params=params, timeout=10)
        if not resp.ok:
            return []
        data = resp.json()
        txs = data.get("result", [])
        if not isinstance(txs, list):
            return []
        transfers = []
        for tx in txs[:limit]:
            try:
                value_wei = int(tx.get("value", "0"))
                value_xdc = value_wei / (10 ** 18)
                from_addr = tx.get("from", "")
                to_addr   = tx.get("to", "")
                direction = "sent" if from_addr.lower() == norm.lower() else "received"
                transfers.append({
                    "direction": direction,
                    "from":      from_addr,
                    "to":        to_addr,
                    "amount":    value_xdc,
                    "amount_formatted": f"{value_xdc:.6f} XDC",
                    "tx_hash":   tx.get("hash", ""),
                    "block":     int(tx.get("blockNumber", 0)),
                    "timestamp": tx.get("timeStamp", ""),
                    "explorer_url": f"https://explorer.xinfin.network/txs/{tx.get('hash', '')}",
                })
            except Exception:
                continue
        return transfers
    except Exception as e:
        logger.error(f"XDC transfer history error for {wallet_address}: {e}")
        return []


def get_xdc_gd_transfer_history(wallet_address: str, limit: int = 50) -> list:
    """
    Fetch XDC G$ (ERC-20) Transfer events directly from the XDC RPC via eth_getLogs.
    This is the authoritative source — it reads directly from the blockchain so the
    token amounts are always correct (blocksscan's tokentx returns native XDC value=0).

    Classifies each event as 'claim' (from UBI Scheme), 'transfer_sent', or
    'transfer_received'. Falls back to empty list on any error.
    """
    import time as _time
    TRANSFER_SIG = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

    norm        = _normalize_xdc_address(wallet_address)
    wallet_lower = norm.lower()
    # 32-byte zero-padded topic for the wallet address
    wallet_topic = "0x" + "0" * 24 + wallet_lower[2:]
    xdc_ubi_lower = XDC_UBI_SCHEME.lower()

    try:
        session = _get_rpc_session()

        # Current XDC block
        blk_resp = session.post(XDC_RPC,
            json={"jsonrpc": "2.0", "method": "eth_blockNumber", "params": [], "id": 0},
            timeout=8)
        to_block_int = int(blk_resp.json()["result"], 16)

        # 14-day window — XDC ≈ 2 sec/block → 43 200 blocks/day
        days_back      = 14
        blocks_per_day = 43_200
        from_block_int = to_block_int - days_back * blocks_per_day

        # Chunk + batch parameters
        chunk_size = 50_000   # 50 k blocks ≈ 28 h on XDC  →  ≈ 13 chunks for 14 days
        batch_size = 5        # 5 chunks per HTTP request   →  ≈ 3 HTTP calls per direction

        raw_logs: list = []
        for wallet_pos in (1, 2):           # 1 = wallet is sender, 2 = wallet is receiver
            topics = [TRANSFER_SIG, None, None]
            topics[wallet_pos] = wallet_topic
            params_tpl = {"address": XDC_GD_TOKEN, "topics": topics}

            chunks = []
            cur_to = to_block_int
            while cur_to >= from_block_int:
                cur_from = max(from_block_int, cur_to - chunk_size + 1)
                chunks.append((cur_from, cur_to))
                cur_to = cur_from - 1

            for batch_start in range(0, len(chunks), batch_size):
                sub = chunks[batch_start: batch_start + batch_size]
                payload = [
                    {"jsonrpc": "2.0", "method": "eth_getLogs",
                     "params": [{**params_tpl, "fromBlock": hex(cf), "toBlock": hex(ct)}],
                     "id": batch_start + i}
                    for i, (cf, ct) in enumerate(sub)
                ]
                try:
                    r = session.post(XDC_RPC, json=payload, timeout=25)
                    results = r.json()
                    if isinstance(results, list):
                        for item in results:
                            raw_logs.extend(item.get("result") or [])
                    elif isinstance(results, dict) and "result" in results:
                        raw_logs.extend(results.get("result") or [])
                except Exception as be:
                    logger.warning(f"[xdc-gd-logs] batch error: {be}")

        # Deduplicate by (txHash, logIndex)
        seen: set = set()
        unique_logs = []
        for log in raw_logs:
            key = (log.get("transactionHash", ""), log.get("logIndex", ""))
            if key not in seen:
                seen.add(key)
                unique_logs.append(log)

        # Sort newest-first, trim
        unique_logs.sort(key=lambda x: int(x.get("blockNumber", "0x0"), 16), reverse=True)
        unique_logs = unique_logs[:limit]

        now_ts = _time.time()
        transfers = []
        for log in unique_logs:
            try:
                tlist = log.get("topics", [])
                if len(tlist) < 3:
                    continue
                from_addr = "0x" + tlist[1][-40:]
                to_addr   = "0x" + tlist[2][-40:]
                block_num = int(log.get("blockNumber", "0x0"), 16)
                tx_hash   = log.get("transactionHash", "")
                try:
                    amount = int(log.get("data", "0x0"), 16) / (10 ** XDC_GD_DECIMALS)
                except Exception:
                    amount = 0.0

                direction = "sent" if from_addr.lower() == wallet_lower else "received"

                if from_addr.lower() == xdc_ubi_lower:
                    tx_type = "claim"
                    label   = "G$ UBI Claim (XDC)"
                elif direction == "sent":
                    tx_type = "transfer_sent"
                    label   = "Sent XDC G$"
                else:
                    tx_type = "transfer_received"
                    label   = "Received XDC G$"

                # Approximate Unix timestamp from block offset (XDC ≈ 2 sec/block)
                approx_ts = now_ts - (to_block_int - block_num) * 2

                transfers.append({
                    "network":          "xdc",
                    "token":            "XDC G$",
                    "direction":        direction,
                    "from":             from_addr,
                    "to":               to_addr,
                    "amount":           float(amount),
                    "amount_formatted": f"{amount:.4f} G$",
                    "block":            block_num,
                    "tx_hash":          tx_hash,
                    "timestamp":        str(int(approx_ts)),   # Unix int as string for frontend
                    "explorer_url":     f"https://xdcscan.io/tx/{tx_hash}",
                    "tx_type":          tx_type,
                    "label":            label,
                    "_sort_ts":         approx_ts,
                })
            except Exception:
                continue

        logger.info(f"[xdc-gd-history] {wallet_address[:8]}… → {len(transfers)} G$ transfers (14d)")
        return transfers

    except Exception as e:
        logger.error(f"XDC G$ transfer history error for {wallet_address}: {e}")
        return []


def prepare_xdc_send_data(to_address: str, amount_xdc: float) -> dict:
    """Prepare native XDC send transaction parameters."""
    try:
        from web3 import Web3
        norm_to = _normalize_xdc_address(to_address)
        to_checksum = Web3.to_checksum_address(norm_to)
        amount_wei = int(amount_xdc * (10 ** 18))
        return {
            "success": True,
            "to": to_checksum,
            "data": "0x",
            "value": hex(amount_wei),
            "chain_id": XDC_CHAIN_ID,
            "token": "XDC",
            "recipient": to_checksum,
            "amount": amount_xdc,
        }
    except Exception as e:
        logger.error(f"prepare_xdc_send_data error: {e}")
        return {"success": False, "error": str(e)}


def prepare_xdc_token_send_data(to_address: str, amount: float, token_contract: str, decimals: int = 6) -> dict:
    """Prepare XDC ERC-20 token transfer calldata."""
    try:
        from web3 import Web3
        from eth_abi import encode as abi_encode
        norm_to = _normalize_xdc_address(to_address)
        to_checksum = Web3.to_checksum_address(norm_to)
        amount_raw = int(amount * (10 ** decimals))
        selector = Web3.keccak(text="transfer(address,uint256)")[:4]
        encoded_args = abi_encode(["address", "uint256"], [to_checksum, amount_raw])
        data = "0x" + (selector + encoded_args).hex()
        return {
            "success": True,
            "to": Web3.to_checksum_address(token_contract),
            "data": data,
            "value": "0x0",
            "chain_id": XDC_CHAIN_ID,
            "token": "XUSDT",
            "recipient": to_checksum,
            "amount": amount,
        }
    except Exception as e:
        logger.error(f"prepare_xdc_token_send_data error: {e}")
        return {"success": False, "error": str(e)}


def get_xdc_gd_balance(wallet_address: str) -> dict:
    """Get G$ (GoodDollar) balance on XDC Network. G$ uses 2 decimals."""
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        norm = _normalize_xdc_address(wallet_address)
        checksum = Web3.to_checksum_address(norm)
        token = Web3.to_checksum_address(XDC_GD_TOKEN)
        abi = [{"inputs":[{"name":"_owner","type":"address"}],"name":"balanceOf","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"}]
        contract = w3.eth.contract(address=token, abi=abi)
        raw = contract.functions.balanceOf(checksum).call()
        balance = raw / (10 ** XDC_GD_DECIMALS)
        return {"success": True, "balance": float(balance), "balance_raw": str(raw), "token": "G$", "network": "xdc"}
    except Exception as e:
        logger.error(f"get_xdc_gd_balance error: {e}")
        return {"success": False, "error": str(e), "balance": 0.0}


def check_xdc_ubi_entitlement(wallet_address: str) -> dict:
    """Check how much G$ the wallet can claim on XDC Network via UBIScheme.checkEntitlement()."""
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        norm = _normalize_xdc_address(wallet_address)
        checksum = Web3.to_checksum_address(norm)
        ubi_addr = Web3.to_checksum_address(XDC_UBI_SCHEME)
        abi = [
            {"inputs":[],"name":"checkEntitlement","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
            {"inputs":[{"name":"_account","type":"address"}],"name":"checkEntitlement","outputs":[{"name":"","type":"uint256"}],"stateMutability":"view","type":"function"},
        ]
        contract = w3.eth.contract(address=ubi_addr, abi=abi)
        try:
            raw = contract.functions.checkEntitlement(checksum).call()
        except Exception:
            raw = contract.functions.checkEntitlement().call({'from': checksum})
        claimable = raw / (10 ** XDC_GD_DECIMALS)
        return {
            "success": True,
            "claimable": float(claimable),
            "claimable_raw": str(raw),
            "can_claim": claimable > 0,
            "network": "xdc",
        }
    except Exception as e:
        logger.error(f"check_xdc_ubi_entitlement error: {e}")
        return {"success": False, "error": str(e), "claimable": 0.0, "can_claim": False}


def is_xdc_identity_whitelisted(wallet_address: str) -> dict:
    """Check if wallet is whitelisted on XDC GoodDollar Identity contract."""
    try:
        from web3 import Web3
        w3 = _get_xdc_w3()
        norm = _normalize_xdc_address(wallet_address)
        checksum = Web3.to_checksum_address(norm)
        id_addr = Web3.to_checksum_address(XDC_IDENTITY)
        abi = [{"inputs":[{"name":"_user","type":"address"}],"name":"isWhitelisted","outputs":[{"name":"","type":"bool"}],"stateMutability":"view","type":"function"}]
        contract = w3.eth.contract(address=id_addr, abi=abi)
        whitelisted = contract.functions.isWhitelisted(checksum).call()
        return {"success": True, "whitelisted": whitelisted, "network": "xdc"}
    except Exception as e:
        logger.error(f"is_xdc_identity_whitelisted error: {e}")
        return {"success": False, "error": str(e), "whitelisted": False}


if __name__ == "__main__":
    test_wallet = "0xFf00A683f7bD77665754A65F2B82fdEFc4371a50"
    result = has_recent_ubi_claim(test_wallet)
    print(result["message"])
