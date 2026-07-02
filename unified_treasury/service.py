"""
UnifiedTreasury service — backend interface for the deployed UnifiedTreasury contract.

The UnifiedTreasury contract holds G$ and distributes to 6 hardcoded recipients.
Only LEARN_WALLET_PRIVATE_KEY (the authorizedSigner) can call distribute().

Key Celo/G$ quirks discovered during development:
  - G$ is ERC-777: eth_call simulations require an explicit gasPrice, otherwise
    they silently revert with no message.
  - contract.distribute() needs ~500k gas for simulation (ERC-777 hook overhead
    when called through a contract layer).
  - Direct wallet transfers need ~250k gas for simulation.
"""
from env_utils import get_env_float, get_env_int

import os
import json
import logging
from pathlib import Path
from web3 import Web3
from eth_account import Account

logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CHAIN_ID = get_env_int("CHAIN_ID", 42220)
GD_TOKEN_ADDRESS = os.getenv(
    "GD_TOKEN_ADDRESS", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"
)

DEPLOYMENT_FILE = (
    Path(__file__).parent.parent / "contracts" / "unified_treasury_deployment.json"
)

GD_ERC20_ABI = [
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
]

RECIPIENT_LABELS = {
    "learn_earn": "Learn & Earn Contract",
    "daily_task": "Daily Task Contract",
    "discourse": "Discourse Wallet",
    "minigames": "Minigames Wallet",
    "community_stories": "Community Stories Wallet",
    "referral": "Referral Wallet",
}


def _load_deployment():
    env_address = os.getenv("UNIFIED_TREASURY_ADDRESS")
    if not DEPLOYMENT_FILE.exists() and not env_address:
        return None, None

    if DEPLOYMENT_FILE.exists():
        try:
            with open(DEPLOYMENT_FILE) as f:
                data = json.load(f)
            return data.get("contract_address"), data.get("abi")
        except Exception as e:
            # Keep the dashboard usable even if the local deployment artifact
            # is missing/corrupted/non-JSON (e.g., accidental overwrite).
            logger.warning(
                "⚠️ Could not parse %s as deployment JSON: %s. "
                "Falling back to UNIFIED_TREASURY_ADDRESS.",
                DEPLOYMENT_FILE,
                e,
            )
            if env_address:
                return env_address, None
            return None, None

    return env_address, None


def get_contract():
    address, abi = _load_deployment()
    if not address:
        return None, None, None

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
    if not w3.is_connected():
        logger.error("❌ Could not connect to Celo RPC")
        return None, None, None

    if not abi:
        abi = [
            {
                "inputs": [
                    {"name": "recipientKey", "type": "string"},
                    {"name": "amount", "type": "uint256"},
                ],
                "name": "distribute",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            },
            {
                "inputs": [{"name": "amount", "type": "uint256"}],
                "name": "deposit",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            },
            {
                "inputs": [],
                "name": "getContractBalance",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [],
                "name": "getStats",
                "outputs": [
                    {"name": "balance", "type": "uint256"},
                    {"name": "deposited", "type": "uint256"},
                    {"name": "distributed", "type": "uint256"},
                ],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [],
                "name": "getAllRecipients",
                "outputs": [
                    {"name": "learnEarn", "type": "address"},
                    {"name": "dailyTask", "type": "address"},
                    {"name": "discourse", "type": "address"},
                    {"name": "minigames", "type": "address"},
                    {"name": "communityStories", "type": "address"},
                    {"name": "referral", "type": "address"},
                ],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [],
                "name": "authorizedSigner",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function",
            },
            {
                "inputs": [],
                "name": "emergencyWithdraw",
                "outputs": [],
                "stateMutability": "nonpayable",
                "type": "function",
            },
        ]

    contract = w3.eth.contract(address=Web3.to_checksum_address(address), abi=abi)
    return w3, contract, address


def get_treasury_status():
    """Return current balance, stats, recipients, and contract address."""
    w3, contract, address = get_contract()
    if not contract:
        return {"configured": False, "error": "UnifiedTreasury not deployed yet"}

    try:
        balance_raw = contract.functions.getContractBalance().call()
        stats = contract.functions.getStats().call()
        recipients_raw = contract.functions.getAllRecipients().call()

        def to_gd(raw):
            return raw / (10**18)

        recipients = {
            "learn_earn": recipients_raw[0],
            "daily_task": recipients_raw[1],
            "discourse": recipients_raw[2],
            "minigames": recipients_raw[3],
            "community_stories": recipients_raw[4],
            "referral": recipients_raw[5],
        }

        return {
            "configured": True,
            "contract_address": address,
            "balance_raw": balance_raw,
            "balance_gd": to_gd(balance_raw),
            "total_deposited": to_gd(stats[1]),
            "total_distributed": to_gd(stats[2]),
            "recipients": recipients,
            "recipient_labels": RECIPIENT_LABELS,
        }
    except Exception as e:
        logger.error(f"❌ Error fetching treasury status: {e}")
        return {"configured": True, "contract_address": address, "error": str(e)}


def distribute_funds(recipient_key: str, amount_gd: float) -> dict:
    """
    Call distribute() on the UnifiedTreasury contract, signed by LEARN_WALLET_PRIVATE_KEY.

    The contract holds G$ and sends to a hardcoded recipient.

    Celo/G$ quirks:
      - eth_call simulation requires gasPrice explicitly set.
      - Contract-level G$ transfer (ERC-777) needs ~500k gas for simulation.

    :param recipient_key: One of learn_earn, daily_task, discourse, minigames,
                          community_stories, referral
    :param amount_gd:     Amount in G$ (human-readable, e.g. 100 for 100 G$)
    :returns: dict with success, tx_hash or error
    """
    if recipient_key not in RECIPIENT_LABELS:
        return {"success": False, "error": f"Unknown recipient: {recipient_key}"}

    learn_key = os.getenv("LEARN_WALLET_PRIVATE_KEY")
    if not learn_key:
        return {"success": False, "error": "LEARN_WALLET_PRIVATE_KEY not configured"}

    w3, contract, address = get_contract()
    if not contract:
        return {"success": False, "error": "UnifiedTreasury contract not configured"}

    try:
        key_hex = learn_key if learn_key.startswith("0x") else "0x" + learn_key
        account = Account.from_key(key_hex)

        # Convert G$ to raw units (18 decimals: 1 G$ = 1e18 raw)
        amount_raw = int(amount_gd * (10**18))
        if amount_raw <= 0:
            return {"success": False, "error": "Amount must be greater than 0"}

        # Check on-chain contract balance
        balance_raw = contract.functions.getContractBalance().call()
        logger.info(
            f"🏦 Treasury balance: {balance_raw / (10**18):,.4f} G$ "
            f"| Requested: {amount_gd:,.4f} G$"
        )

        if balance_raw < amount_raw:
            return {
                "success": False,
                "error": (
                    f"Insufficient treasury balance. "
                    f"Available: {balance_raw / (10**18):,.2f} G$, "
                    f"Requested: {amount_gd:,.2f} G$. "
                    f"Please deposit G$ into the treasury contract first."
                ),
            }

        nonce = w3.eth.get_transaction_count(account.address)
        gas_price = w3.eth.gas_price

        # 500k gas: contract → G$ token (ERC-777) adds hook overhead
        tx = contract.functions.distribute(recipient_key, amount_raw).build_transaction(
            {
                "from": account.address,
                "nonce": nonce,
                "gas": 500_000,
                "gasPrice": gas_price,
                "chainId": CHAIN_ID,
            }
        )

        # Dry-run simulation
        # IMPORTANT: gasPrice must be set explicitly — Celo's G$ ERC-777 token
        # reverts silently in eth_call when gasPrice is omitted.
        try:
            w3.eth.call(
                {
                    "from": account.address,
                    "to": contract.address,
                    "data": tx["data"],
                    "gas": tx["gas"],
                    "gasPrice": gas_price,
                }
            )
        except Exception as sim_err:
            err_str = str(sim_err)
            import re

            match = re.search(r"revert\s+(.+?)(?:'|\"|\Z)", err_str, re.IGNORECASE)
            revert_reason = match.group(1).strip() if match else err_str
            logger.error(f"❌ Simulation reverted: {err_str}")
            return {
                "success": False,
                "error": f"Transaction would revert: {revert_reason}",
            }

        # Sign and broadcast
        signed = account.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        logger.info(f"📡 Sent tx: {tx_hash.hex()}")
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

        if receipt.status != 1:
            try:
                w3.eth.call(
                    {
                        "from": account.address,
                        "to": contract.address,
                        "data": tx["data"],
                        "gasPrice": gas_price,
                    },
                    block_identifier=receipt.blockNumber,
                )
            except Exception as revert_err:
                logger.error(f"❌ On-chain revert: {revert_err}")
                return {
                    "success": False,
                    "error": f"Transaction reverted: {str(revert_err)}",
                }
            return {
                "success": False,
                "error": "Transaction reverted on-chain (unknown reason)",
            }

        logger.info(
            f"✅ Distributed {amount_gd:,.4f} G$ → {RECIPIENT_LABELS[recipient_key]} "
            f"| tx: {tx_hash.hex()}"
        )
        return {
            "success": True,
            "tx_hash": tx_hash.hex(),
            "recipient_key": recipient_key,
            "recipient_label": RECIPIENT_LABELS[recipient_key],
            "amount_gd": amount_gd,
            "block": receipt.blockNumber,
        }

    except Exception as e:
        logger.error(f"❌ distribute_funds error: {e}")
        return {"success": False, "error": str(e)}
