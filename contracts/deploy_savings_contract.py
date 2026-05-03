"""
GDSavings Contract Deployment Script for Celo Mainnet (v4)

Deploys the multi-token GDSavings vault (no owner, no pause, no early
withdrawal). Tokens accepted: G$, CELO, cUSD.

Features:
  - One slot per (user, token, lockDays). Top-ups inherit the slot's
    original unlocksAt (no lock extension).
  - Lock durations (days): 1, 30, 60, 90, 120, 150, 180, 210, 240, 270,
    300, 330, 365.
  - Per-token min/max (18-decimal units):
      G$:   1,000        – 10,000,000
      CELO: 1            – 100,000
      cUSD: 1            – 1,000,000
  - Per-duration bonus structure (always paid in G$, regardless of
    deposit token; internal contract ratio 1 G$ ≡ 0.001 CELO ≡ 0.001 cUSD):
      1-day  → 30 G$ if amount ≥ per-token MIN.
      30..330d (multiples of 30) → (lockDays / 30) * 500 G$ if amount
                                  ≥ per-token "100k G$ equivalent"
                                  (G$ 100,000 / CELO 100 / cUSD 100).
      365d   → 20,000 G$ if amount ≥ per-token "1M G$ equivalent"
               (G$ 1,000,000 / CELO 1,000 / cUSD 1,000).
  - Anyone can fund the G$ reward pool via fundRewardPool().
  - No owner, no admin, no pause, no emergency, no early withdrawal.
"""

import os
import json
import logging
from web3 import Web3
from eth_account import Account
from solcx import compile_standard, install_solc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))

# Token addresses on Celo Mainnet (override via env if deploying to a fork/testnet).
GOODDOLLAR_CONTRACT = os.getenv(
    'GOODDOLLAR_CONTRACT_ADDRESS',
    '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A',
)
CELO_TOKEN_ADDRESS = os.getenv(
    'CELO_TOKEN_ADDRESS',
    '0x471EcE3750Da237f93B8E339c536989b8978a438',
)
CUSD_TOKEN_ADDRESS = os.getenv(
    'CUSD_TOKEN_ADDRESS',
    '0x765DE816845861e75A25fCA122bb6898B8B1282a',
)

FLATTENED_SOURCE = open(os.path.join(os.path.dirname(__file__), 'GDSavings.sol')).read()


def compile_contract():
    logger.info("Installing Solidity compiler v0.8.21...")
    install_solc('0.8.21')
    logger.info("Compiling GDSavings contract...")

    compiled = compile_standard({
        "language": "Solidity",
        "sources": {
            "GDSavings.sol": {"content": FLATTENED_SOURCE}
        },
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {
                "*": {
                    "*": ["abi", "metadata", "evm.bytecode", "evm.deployedBytecode"]
                }
            }
        }
    }, solc_version='0.8.21')

    contract_data = compiled["contracts"]["GDSavings.sol"]["GDSavings"]
    return {
        "abi": contract_data["abi"],
        "bytecode": contract_data["evm"]["bytecode"]["object"]
    }


def deploy_contract():
    saving_key = os.getenv('SAVING_KEY')

    if not saving_key:
        logger.error("SAVING_KEY not set!")
        return None

    if not GOODDOLLAR_CONTRACT:
        logger.error("GOODDOLLAR_CONTRACT_ADDRESS not set!")
        return None
    if not CELO_TOKEN_ADDRESS:
        logger.error("CELO_TOKEN_ADDRESS not set!")
        return None
    if not CUSD_TOKEN_ADDRESS:
        logger.error("CUSD_TOKEN_ADDRESS not set!")
        return None

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
    if not w3.is_connected():
        logger.error("Failed to connect to Celo network")
        return None

    logger.info(f"Connected to Celo Mainnet (Chain ID: {CHAIN_ID})")

    key = saving_key if saving_key.startswith('0x') else '0x' + saving_key
    account = Account.from_key(key)
    logger.info(f"Deploying from SAVING_KEY address: {account.address}")
    logger.info(f"  G$   token: {GOODDOLLAR_CONTRACT}")
    logger.info(f"  CELO token: {CELO_TOKEN_ADDRESS}")
    logger.info(f"  cUSD token: {CUSD_TOKEN_ADDRESS}")

    celo_balance = w3.eth.get_balance(account.address)
    celo_human = w3.from_wei(celo_balance, 'ether')
    logger.info(f"CELO balance: {celo_human} CELO")

    if celo_balance < w3.to_wei(0.05, 'ether'):
        logger.error(f"Insufficient CELO for gas (need ~0.05, have {celo_human}). Top up the SAVING_KEY address.")
        return None

    compiled = compile_contract()

    contract = w3.eth.contract(
        abi=compiled["abi"],
        bytecode=compiled["bytecode"]
    )

    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = int(w3.eth.gas_price * 1.2)

    # Estimate gas dynamically rather than hardcoding (Celo gas spikes
    # could otherwise turn a 3.5M*price ceiling into "insufficient funds"
    # even when the actual gas usage is only ~2M).
    ctor_call = contract.constructor(
        Web3.to_checksum_address(GOODDOLLAR_CONTRACT),
        Web3.to_checksum_address(CELO_TOKEN_ADDRESS),
        Web3.to_checksum_address(CUSD_TOKEN_ADDRESS),
    )
    try:
        gas_estimate = ctor_call.estimate_gas({'from': account.address})
    except Exception as e:
        logger.warning(f"estimate_gas failed ({e}); falling back to 2_500_000")
        gas_estimate = 2_500_000
    gas_limit = int(gas_estimate * 1.15)
    logger.info(f"Gas estimate: {gas_estimate} (using limit: {gas_limit})")
    logger.info(f"Gas price:    {gas_price} wei (~{gas_price / 1e9:.2f} gwei)")
    logger.info(f"Max tx cost:  {gas_limit * gas_price} wei (~{gas_limit * gas_price / 1e18:.4f} CELO)")

    constructor_txn = ctor_call.build_transaction({
        'chainId':  CHAIN_ID,
        'gas':      gas_limit,
        'gasPrice': gas_price,
        'nonce':    nonce,
    })

    signed_txn = w3.eth.account.sign_transaction(constructor_txn, key)
    tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
    tx_hash_hex = tx_hash.hex()
    if not tx_hash_hex.startswith('0x'):
        tx_hash_hex = '0x' + tx_hash_hex

    logger.info(f"Tx hash: {tx_hash_hex}")
    logger.info(f"Explorer: https://celoscan.io/tx/{tx_hash_hex}")
    logger.info("Waiting for confirmation...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt.status == 1:
        contract_address = receipt.contractAddress
        logger.info(f"✅ Contract deployed: {contract_address}")
        logger.info(f"   CeloScan: https://celoscan.io/address/{contract_address}")
        logger.info(f"   Gas used: {receipt.gasUsed}")

        deployment_info = {
            "contract_name": "GDSavings",
            "version": "4",
            "contract_address": contract_address,
            "tx_hash": tx_hash_hex,
            "deployer": account.address,
            "gooddollar_token": GOODDOLLAR_CONTRACT,
            "celo_token": CELO_TOKEN_ADDRESS,
            "cusd_token": CUSD_TOKEN_ADDRESS,
            "chain_id": CHAIN_ID,
            "network": "Celo Mainnet",
            "block_number": receipt.blockNumber,
            "gas_used": receipt.gasUsed,
            "compiler_version": "v0.8.21+commit.d9974bed",
            "optimization": True,
            "optimization_runs": 200,
            "notes": (
                "Multi-token (G$, CELO, cUSD). Per-(user, token, lockDays) slot. "
                "Top-ups inherit the slot's unlocksAt. No early withdrawal. "
                "No owner, no pause. Reward pool is G$-only and trustless."
            ),
            "abi": compiled["abi"]
        }

        out = os.path.join(os.path.dirname(__file__), 'savings_deployment_info.json')
        with open(out, 'w') as f:
            json.dump(deployment_info, f, indent=2)

        logger.info(f"Deployment info saved to: {out}")
        logger.info(f"\nSet this env variable:")
        logger.info(f"  SAVINGS_CONTRACT_ADDRESS={contract_address}")

        return deployment_info
    else:
        logger.error("❌ Deployment failed!")
        return None


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("GDSavings Contract Deployment — Celo Mainnet")
    logger.info("=" * 60)
    result = deploy_contract()
    if result:
        logger.info("\n✅ DEPLOYMENT SUCCESSFUL!")
        logger.info(f"Contract:  {result['contract_address']}")
        logger.info(f"Deployer:  {result['deployer']}")
        logger.info(f"Set env:   SAVINGS_CONTRACT_ADDRESS={result['contract_address']}")
    else:
        logger.error("Deployment failed.")
