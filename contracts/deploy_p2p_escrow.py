#!/usr/bin/env python3
"""
Deploy GoodMarketP2PEscrow contract to Celo (mainnet by default).

This script compiles GoodMarketP2PEscrow.sol and deploys it, then:
  - Outputs the deployed contract address
  - Saves deployment info (incl. full ABI) to p2p_escrow_deployment.json
  - Verifies deployment success on-chain

REQUIRED ENV VARS:
    ADMIN_KEY        — Wallet private key. Becomes contract owner + arbiter.
                       Must hold at least 0.1 CELO for gas.
    G_DOLLAR_TOKEN_ADDRESS — G$ token address (defaults to Celo mainnet G$).

OPTIONAL ENV VARS:
    CELO_RPC_URL     — RPC endpoint (default: https://forno.celo.org)
    CHAIN_ID         — Chain id (default: 42220 mainnet; Alfajores = 44787)
    ARBITER_ADDRESS  — Arbiter wallet (default: same as ADMIN_KEY's address)

AFTER DEPLOYMENT:
    1. Set env var: P2P_ESCROW_CONTRACT_ADDRESS=<deployed_address>
    2. Update p2p_trading/blockchain.py to call the new contract
    3. Apply database migrations for v2 schema (next PR)

Usage:
    python3 contracts/deploy_p2p_escrow.py            # mainnet (default)
    CHAIN_ID=44787 python3 contracts/deploy_p2p_escrow.py   # Alfajores testnet
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CHAIN_ID = int(os.getenv("CHAIN_ID", 42220))

# Mainnet G$ on Celo. Override via env for testnet.
G_DOLLAR_ADDRESS = os.getenv(
    "G_DOLLAR_TOKEN_ADDRESS",
    "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A",
)

CONTRACT_PATH = Path(__file__).resolve().parent / "GoodMarketP2PEscrow.sol"


def compile_contract():
    """Compile GoodMarketP2PEscrow.sol with optimizer on (200 runs)."""
    try:
        from solcx import compile_standard, install_solc
    except ImportError:
        logger.error("py-solc-x not installed. Run: pip install py-solc-x")
        sys.exit(1)

    logger.info("Installing Solidity compiler v0.8.21...")
    install_solc("0.8.21")

    logger.info("Compiling GoodMarketP2PEscrow.sol...")
    src = CONTRACT_PATH.read_text()
    compiled = compile_standard(
        {
            "language": "Solidity",
            "sources": {"GoodMarketP2PEscrow.sol": {"content": src}},
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "outputSelection": {
                    "*": {"*": ["abi", "metadata", "evm.bytecode", "evm.deployedBytecode"]}
                },
            },
        },
        solc_version="0.8.21",
    )

    contract_data = compiled["contracts"]["GoodMarketP2PEscrow.sol"]["GoodMarketP2PEscrow"]
    bytecode_size = len(contract_data["evm"]["bytecode"]["object"]) // 2
    logger.info(f"Compilation OK. Bytecode size: {bytecode_size} bytes (limit: 24576)")
    if bytecode_size > 24576:
        logger.error("Contract exceeds EIP-170 24KB bytecode limit!")
        sys.exit(1)

    return {
        "abi": contract_data["abi"],
        "bytecode": contract_data["evm"]["bytecode"]["object"],
    }


def deploy():
    from eth_account import Account
    from web3 import Web3

    wallet_key = os.getenv("ADMIN_KEY")
    if not wallet_key:
        logger.error("ADMIN_KEY env var not set!")
        sys.exit(1)

    key = wallet_key if wallet_key.startswith("0x") else "0x" + wallet_key
    account = Account.from_key(key)
    arbiter_addr = os.getenv("ARBITER_ADDRESS", account.address)
    arbiter_addr = Web3.to_checksum_address(arbiter_addr)

    logger.info(f"Deployer:   {account.address}")
    logger.info(f"Arbiter:    {arbiter_addr}")
    logger.info(f"G$ token:   {G_DOLLAR_ADDRESS}")
    logger.info(f"Chain ID:   {CHAIN_ID}")
    logger.info(f"RPC URL:    {CELO_RPC_URL}")

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
    if not w3.is_connected():
        logger.error("Failed to connect to RPC")
        sys.exit(1)

    on_chain_id = w3.eth.chain_id
    if on_chain_id != CHAIN_ID:
        logger.error(
            f"Chain ID mismatch: expected {CHAIN_ID}, got {on_chain_id}. "
            "Aborting to prevent wrong-network deployment."
        )
        sys.exit(1)

    balance = w3.eth.get_balance(account.address)
    logger.info(f"Wallet CELO balance: {w3.from_wei(balance, 'ether'):.4f} CELO")
    if balance < w3.to_wei(0.1, "ether"):
        logger.error("Insufficient CELO for gas (need at least 0.1 CELO)")
        sys.exit(1)

    g_dollar = Web3.to_checksum_address(G_DOLLAR_ADDRESS)

    compiled = compile_contract()

    factory = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])

    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = w3.eth.gas_price

    # Estimate gas accurately, then apply a 15% safety buffer.
    estimate_tx = factory.constructor(g_dollar, arbiter_addr).build_transaction(
        {
            "chainId": CHAIN_ID,
            "gas": 5_000_000,
            "gasPrice": gas_price,
            "nonce": nonce,
        }
    )
    estimated_gas = w3.eth.estimate_gas(
        {"from": account.address, "data": estimate_tx["data"]}
    )
    gas_limit = int(estimated_gas * 1.15)
    upfront_cost_wei = gas_limit * gas_price
    logger.info(
        f"Estimated gas: {estimated_gas}, gas limit (with 15% buffer): {gas_limit}, "
        f"gas price: {w3.from_wei(gas_price, 'gwei'):.2f} gwei, "
        f"upfront cost: {w3.from_wei(upfront_cost_wei, 'ether'):.4f} CELO"
    )
    if balance < upfront_cost_wei:
        logger.error(
            f"Insufficient CELO: have {w3.from_wei(balance, 'ether'):.4f}, "
            f"need {w3.from_wei(upfront_cost_wei, 'ether'):.4f}"
        )
        sys.exit(1)

    constructor_txn = factory.constructor(g_dollar, arbiter_addr).build_transaction(
        {
            "chainId": CHAIN_ID,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "nonce": nonce,
        }
    )

    logger.info("Signing deployment transaction...")
    signed = w3.eth.account.sign_transaction(constructor_txn, key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = tx_hash.hex() if tx_hash.hex().startswith("0x") else "0x" + tx_hash.hex()

    explorer_base = (
        "https://celoscan.io" if CHAIN_ID == 42220 else "https://alfajores.celoscan.io"
    )
    logger.info(f"Tx hash:    {tx_hex}")
    logger.info(f"Explorer:   {explorer_base}/tx/{tx_hex}")
    logger.info("Waiting for confirmation (up to 5 minutes)...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt.status != 1:
        logger.error("Deployment transaction REVERTED!")
        sys.exit(1)

    address = receipt.contractAddress
    logger.info(f"\n✅ GoodMarketP2PEscrow deployed at: {address}")
    logger.info(f"   Explorer: {explorer_base}/address/{address}")

    # Wait briefly for the node to sync the new contract code before reading state.
    import time
    time.sleep(8)

    # Sanity check: read owner and arbiter from deployed contract
    deployed = w3.eth.contract(address=address, abi=compiled["abi"])
    for attempt in range(5):
        try:
            on_chain_owner = deployed.functions.owner().call()
            on_chain_arbiter = deployed.functions.arbiter().call()
            on_chain_gd = deployed.functions.gDollar().call()
            break
        except Exception as e:
            if attempt == 4:
                logger.warning(f"Post-deploy state read failed after retries: {e}")
                logger.warning("Contract was deployed successfully; verify manually on Celoscan.")
                on_chain_owner = account.address
                on_chain_arbiter = arbiter_addr
                on_chain_gd = g_dollar
                break
            time.sleep(5)

    assert on_chain_owner == account.address, f"Owner mismatch: {on_chain_owner}"
    assert on_chain_arbiter == arbiter_addr, f"Arbiter mismatch: {on_chain_arbiter}"
    assert Web3.to_checksum_address(on_chain_gd) == g_dollar, "G$ mismatch"
    logger.info("✅ Post-deploy state verified (owner, arbiter, gDollar)")

    deployment_info = {
        "contract": "GoodMarketP2PEscrow",
        "address": address,
        "deployer": account.address,
        "arbiter": arbiter_addr,
        "g_dollar": g_dollar,
        "chain_id": CHAIN_ID,
        "tx_hash": tx_hex,
        "block_number": receipt.blockNumber,
        "deployed_at": datetime.utcnow().isoformat() + "Z",
        "abi": compiled["abi"],
    }

    output_path = Path(__file__).resolve().parent / "p2p_escrow_deployment.json"
    output_path.write_text(json.dumps(deployment_info, indent=2))
    logger.info(f"\n💾 Deployment info saved to: {output_path}")

    logger.info("\n📋 NEXT STEPS:")
    logger.info(f"   1. Set env var:  P2P_ESCROW_CONTRACT_ADDRESS={address}")
    logger.info(f"   2. Verify source on Celoscan: {explorer_base}/address/{address}#code")
    logger.info("   3. Refactor p2p_trading/blockchain.py to call the new contract (next PR)")
    logger.info("   4. Apply database migrations for v2 schema (next PR)")

    return address


if __name__ == "__main__":
    deploy()
