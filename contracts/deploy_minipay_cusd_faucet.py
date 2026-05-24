#!/usr/bin/env python3
"""Deploy GoodMarketMiniPayCUSDFaucet to Celo-compatible network.

Usage:
  export CELO_RPC=...
  export TOPWALLET_KEY=0x...
  python contracts/deploy_minipay_cusd_faucet.py \
    --cusd 0x765DE816845861e75A25fCA122bb6898B8B1282a \
    --disburser 0xYourBackendHotWallet \
    --cooldown-seconds 172800
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from eth_account import Account
from solcx import compile_standard, install_solc
from web3 import Web3

DEFAULT_OUT = Path(__file__).with_name("minipay_cusd_faucet_deployment.json")
DEFAULT_SOLC = "0.8.20"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Deploy GoodMarketMiniPayCUSDFaucet")
    parser.add_argument("--cusd", required=True, help="cUSD token address")
    parser.add_argument("--disburser", required=True, help="Fixed disburser wallet (backend signer / TOPWALLET_KEY address)")
    parser.add_argument("--cooldown-seconds", type=int, default=172800, help="Per-recipient on-chain cooldown in seconds")
    parser.add_argument("--chain-id", type=int, default=42220, help="Chain ID (default: 42220 Celo mainnet)")
    parser.add_argument("--confirmations", type=int, default=1, help="Receipt wait confirmations")
    parser.add_argument("--out", default=str(DEFAULT_OUT), help=f"Deployment metadata output path (default: {DEFAULT_OUT})")
    return parser.parse_args()


def load_contract_artifact(solidity_file: Path) -> tuple[list, str]:
    source = solidity_file.read_text(encoding="utf-8")
    install_solc(DEFAULT_SOLC)
    compiled = compile_standard(
        {
            "language": "Solidity",
            "sources": {solidity_file.name: {"content": source}},
            "settings": {
                "optimizer": {"enabled": True, "runs": 200},
                "outputSelection": {"*": {"*": ["abi", "evm.bytecode"]}},
            },
        },
        solc_version=DEFAULT_SOLC,
    )
    contract_data = compiled["contracts"][solidity_file.name]["GoodMarketMiniPayCUSDFaucet"]
    abi = contract_data["abi"]
    bytecode = contract_data["evm"]["bytecode"]["object"]
    if not bytecode:
        raise RuntimeError("Compiled bytecode is empty")
    return abi, bytecode


def main() -> None:
    args = parse_args()

    rpc = os.getenv("CELO_RPC")
    if not rpc:
        raise RuntimeError("Missing CELO_RPC env var")

    key = (os.getenv("TOPWALLET_KEY") or "").strip()
    if not key:
        raise RuntimeError("Missing TOPWALLET_KEY env var")
    if not key.startswith("0x"):
        key = "0x" + key

    w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise RuntimeError("Failed to connect to CELO_RPC")

    deployer = Account.from_key(key)
    cusd = Web3.to_checksum_address(args.cusd)
    disburser = Web3.to_checksum_address(args.disburser)

    solidity_file = Path(__file__).with_name("GoodMarketMiniPayCUSDFaucet.sol")
    abi, bytecode = load_contract_artifact(solidity_file)

    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(deployer.address, "pending")
    gas_price = int(w3.eth.gas_price)

    tx = contract.constructor(cusd, disburser, args.cooldown_seconds).build_transaction(
        {
            "from": deployer.address,
            "nonce": nonce,
            "chainId": args.chain_id,
            "gasPrice": gas_price,
        }
    )
    tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)

    signed = deployer.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt.status != 1:
        raise RuntimeError(f"Deployment failed, tx={tx_hash.hex()}")

    deployment = {
        "contract_name": "GoodMarketMiniPayCUSDFaucet",
        "deployed_at": datetime.now(timezone.utc).isoformat(),
        "chain_id": args.chain_id,
        "network_rpc": rpc,
        "deployer": deployer.address,
        "disburser": disburser,
        "cooldown_seconds": args.cooldown_seconds,
        "cusd": cusd,
        "tx_hash": tx_hash.hex(),
        "contract_address": receipt.contractAddress,
        "block_number": receipt.blockNumber,
        "gas_used": receipt.gasUsed,
        "abi": abi,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(deployment, indent=2), encoding="utf-8")

    print("✅ Deployment successful")
    print(f"Contract: {receipt.contractAddress}")
    print(f"Tx Hash : {tx_hash.hex()}")
    print(f"Saved   : {out_path}")


if __name__ == "__main__":
    main()
