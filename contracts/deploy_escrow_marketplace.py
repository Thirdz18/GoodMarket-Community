#!/usr/bin/env python3
"""
Deploy EscrowMarketplace contract to Celo Mainnet.

This script compiles EscrowMarketplace.sol and deploys it, then:
  - Outputs the deployed contract address
  - Saves deployment info to escrow_marketplace_deployment.json

REQUIRED ENV VARS:
    LEARN_WALLET_PRIVATE_KEY          — App operator wallet (pays gas, becomes contract owner)
    ACHIEVEMENT_NFT_CONTRACT_ADDRESS  — Address of already-deployed AchievementNFT
    G_DOLLAR_TOKEN_ADDRESS            — G$ token address (default: Celo mainnet G$)

AFTER DEPLOYMENT:
    1. Set env var:  ESCROW_MARKETPLACE_ADDRESS=<deployed_address>
    2. Call on AchievementNFT:
         AchievementNFT.setMarketplaceOperator(escrowContractAddress)
       (Can use scripts/set_marketplace_operator.py or CeloScan write interface)

Usage:
    python3 contracts/deploy_escrow_marketplace.py
"""

import os
import sys
import json
import logging
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))
G_DOLLAR_ADDRESS = os.getenv('G_DOLLAR_TOKEN_ADDRESS', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')

ESCROW_SOURCE = open(os.path.join(os.path.dirname(__file__), 'EscrowMarketplace.sol')).read()

ESCROW_ABI = [
    {
        "inputs": [
            {"name": "_nftContract", "type": "address"},
            {"name": "_gdToken", "type": "address"}
        ],
        "stateMutability": "nonpayable",
        "type": "constructor"
    },
    {
        "inputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "seller", "type": "address"},
            {"name": "priceG", "type": "uint256"}
        ],
        "name": "listNFT",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "cancelListing",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "tokenId", "type": "uint256"},
            {"name": "buyer", "type": "address"}
        ],
        "name": "completeSwap",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "tokenId", "type": "uint256"}],
        "name": "getListing",
        "outputs": [
            {"name": "seller", "type": "address"},
            {"name": "priceG", "type": "uint256"},
            {"name": "active", "type": "bool"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "buyer", "type": "address"}],
        "name": "getAllowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "newOwner", "type": "address"}],
        "name": "transferOwnership",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "_nftContract", "type": "address"},
            {"name": "_gdToken", "type": "address"}
        ],
        "name": "updateContracts",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "owner",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "nftContract",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "gdToken",
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "tokenId", "type": "uint256"},
            {"indexed": True, "name": "seller", "type": "address"},
            {"indexed": False, "name": "priceG", "type": "uint256"}
        ],
        "name": "NFTListed",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "tokenId", "type": "uint256"},
            {"indexed": True, "name": "seller", "type": "address"}
        ],
        "name": "ListingCancelled",
        "type": "event"
    },
    {
        "anonymous": False,
        "inputs": [
            {"indexed": True, "name": "tokenId", "type": "uint256"},
            {"indexed": True, "name": "buyer", "type": "address"},
            {"indexed": True, "name": "seller", "type": "address"},
            {"indexed": False, "name": "priceG", "type": "uint256"}
        ],
        "name": "SwapCompleted",
        "type": "event"
    }
]


def compile_contract():
    try:
        from solcx import compile_standard, install_solc
    except ImportError:
        logger.error("py-solc-x not installed. Run: pip install py-solc-x")
        sys.exit(1)

    logger.info("Installing Solidity compiler v0.8.21...")
    install_solc('0.8.21')

    logger.info("Compiling EscrowMarketplace.sol...")
    compiled = compile_standard({
        "language": "Solidity",
        "sources": {
            "EscrowMarketplace.sol": {"content": ESCROW_SOURCE}
        },
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {
                "*": {"*": ["abi", "metadata", "evm.bytecode", "evm.deployedBytecode"]}
            }
        }
    }, solc_version='0.8.21')

    contract_data = compiled["contracts"]["EscrowMarketplace.sol"]["EscrowMarketplace"]
    logger.info("Compilation successful.")
    return {
        "abi": contract_data["abi"],
        "bytecode": contract_data["evm"]["bytecode"]["object"]
    }


def deploy_contract():
    from web3 import Web3
    from eth_account import Account

    wallet_key = os.getenv('LEARN_WALLET_PRIVATE_KEY')
    nft_address = os.getenv('ACHIEVEMENT_NFT_CONTRACT_ADDRESS')

    if not wallet_key:
        logger.error("LEARN_WALLET_PRIVATE_KEY not set!")
        return None
    if not nft_address:
        logger.error("ACHIEVEMENT_NFT_CONTRACT_ADDRESS not set!")
        return None

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))
    if not w3.is_connected():
        logger.error("Failed to connect to Celo network")
        return None

    logger.info(f"Connected to Celo (chain {CHAIN_ID})")

    key = wallet_key if wallet_key.startswith('0x') else '0x' + wallet_key
    account = Account.from_key(key)
    logger.info(f"Deploying from: {account.address}")

    celo_balance = w3.eth.get_balance(account.address)
    logger.info(f"CELO balance: {w3.from_wei(celo_balance, 'ether'):.4f} CELO")
    if celo_balance < w3.to_wei(0.01, 'ether'):
        logger.error("Insufficient CELO for gas (need at least 0.01 CELO)")
        return None

    nft_checksum  = Web3.to_checksum_address(nft_address)
    g_checksum    = Web3.to_checksum_address(G_DOLLAR_ADDRESS)
    logger.info(f"NFT contract:  {nft_checksum}")
    logger.info(f"G$ token:      {g_checksum}")

    compiled = compile_contract()

    factory = w3.eth.contract(abi=compiled["abi"], bytecode=compiled["bytecode"])

    nonce     = w3.eth.get_transaction_count(account.address)
    gas_price = int(w3.eth.gas_price * 1.2)

    constructor_txn = factory.constructor(
        nft_checksum,
        g_checksum
    ).build_transaction({
        'chainId': CHAIN_ID,
        'gas': 1_500_000,
        'gasPrice': gas_price,
        'nonce': nonce,
    })

    logger.info("Signing and sending deployment transaction...")
    signed = w3.eth.account.sign_transaction(constructor_txn, key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex = '0x' + tx_hash.hex() if not tx_hash.hex().startswith('0x') else tx_hash.hex()

    logger.info(f"Tx hash:    {tx_hex}")
    logger.info(f"Explorer:   https://celoscan.io/tx/{tx_hex}")
    logger.info("Waiting for confirmation (up to 5 minutes)...")

    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt.status != 1:
        logger.error("Deployment transaction REVERTED!")
        return None

    escrow_address = receipt.contractAddress
    logger.info(f"\n✅ EscrowMarketplace deployed at: {escrow_address}")
    logger.info(f"   Explorer: https://celoscan.io/address/{escrow_address}")
    logger.info(f"\n📋 NEXT STEPS:")
    logger.info(f"   1. Set env var: ESCROW_MARKETPLACE_ADDRESS={escrow_address}")
    logger.info(f"   2. Call AchievementNFT.setMarketplaceOperator(\"{escrow_address}\")")
    logger.info(f"      (Use CeloScan write interface or run set_marketplace_operator.py)")

    deployment_info = {
        "contract": "EscrowMarketplace",
        "address": escrow_address,
        "deployer": account.address,
        "nft_contract": nft_checksum,
        "gd_token": g_checksum,
        "chain_id": CHAIN_ID,
        "tx_hash": tx_hex,
        "block_number": receipt.blockNumber,
        "deployed_at": datetime.utcnow().isoformat() + "Z",
        "abi": ESCROW_ABI
    }

    output_path = os.path.join(os.path.dirname(__file__), 'escrow_marketplace_deployment.json')
    with open(output_path, 'w') as f:
        json.dump(deployment_info, f, indent=2)
    logger.info(f"\n💾 Deployment info saved to: {output_path}")

    return escrow_address


def set_marketplace_operator(escrow_address: str):
    """
    Call AchievementNFT.setMarketplaceOperator(escrowAddress) from the owner wallet.
    Run this AFTER deploying the escrow contract.
    """
    from web3 import Web3
    from eth_account import Account

    wallet_key  = os.getenv('LEARN_WALLET_PRIVATE_KEY')
    nft_address = os.getenv('ACHIEVEMENT_NFT_CONTRACT_ADDRESS')

    if not wallet_key or not nft_address:
        logger.error("Missing LEARN_WALLET_PRIVATE_KEY or ACHIEVEMENT_NFT_CONTRACT_ADDRESS")
        return False

    w3   = Web3(Web3.HTTPProvider(CELO_RPC_URL))
    key  = wallet_key if wallet_key.startswith('0x') else '0x' + wallet_key
    acct = Account.from_key(key)

    set_operator_abi = [{
        "inputs": [{"name": "newOperator", "type": "address"}],
        "name": "setMarketplaceOperator",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    }]

    nft = w3.eth.contract(address=Web3.to_checksum_address(nft_address), abi=set_operator_abi)
    nonce     = w3.eth.get_transaction_count(acct.address)
    gas_price = int(w3.eth.gas_price * 1.2)

    txn = nft.functions.setMarketplaceOperator(
        Web3.to_checksum_address(escrow_address)
    ).build_transaction({
        'chainId': CHAIN_ID,
        'gas': 100_000,
        'gasPrice': gas_price,
        'nonce': nonce,
    })

    signed  = w3.eth.account.sign_transaction(txn, key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    tx_hex  = tx_hash.hex()
    if not tx_hex.startswith('0x'):
        tx_hex = '0x' + tx_hex

    logger.info(f"setMarketplaceOperator tx: {tx_hex}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status == 1:
        logger.info(f"✅ AchievementNFT.setMarketplaceOperator({escrow_address}) succeeded!")
        return True
    else:
        logger.error("setMarketplaceOperator transaction reverted!")
        return False


if __name__ == '__main__':
    escrow_addr = deploy_contract()
    if escrow_addr:
        answer = input("\nCall setMarketplaceOperator on AchievementNFT now? [y/N]: ").strip().lower()
        if answer == 'y':
            set_marketplace_operator(escrow_addr)
