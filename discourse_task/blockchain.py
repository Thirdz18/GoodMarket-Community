
from env_utils import get_env_float, get_env_int
import os
import asyncio
import logging
from web3 import Web3
from eth_account import Account

logger = logging.getLogger(__name__)

class DiscourseTaskBlockchain:
    """Discourse Task Direct Private Key Disbursement"""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = get_env_int('CHAIN_ID', 42220)
        self.gooddollar_contract = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')

        # Discourse_Task key
        self.task_key = os.getenv('DISCOURSE_TASK_KEY')

        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("✅ Connected to Celo network for Discourse Task")
        else:
            logger.error("❌ Failed to connect to Celo network for Discourse Task")

        logger.info("💬 Discourse Task Blockchain Service initialized")

    def mask_wallet_address(self, wallet_address: str) -> str:
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def disburse_discourse_reward(self, wallet_address: str, amount: float) -> dict:
        """Disburse Discourse Task rewards via direct private key transfer"""
        try:
            masked_wallet = self.mask_wallet_address(wallet_address)
            logger.info(f"💬 Discourse Task reward disbursement: {amount} G$ to {masked_wallet}")

            if not self.task_key:
                logger.error("❌ DISCOURSE_TASK_KEY not configured")
                return {"success": False, "error": "Discourse_Task key not configured"}

            if not self.w3.is_connected():
                logger.error("❌ Not connected to Celo network")
                return {"success": False, "error": "Blockchain connection failed"}

            try:
                key = self.task_key if self.task_key.startswith('0x') else '0x' + self.task_key
                task_account = Account.from_key(key)
                logger.info(f"🔑 Using Discourse Task account: {self.mask_wallet_address(task_account.address)}")
            except Exception as key_error:
                logger.error(f"❌ Failed to load DISCOURSE_TASK_KEY: {key_error}")
                return {"success": False, "error": "Key loading error"}

            erc20_abi = [
                {
                    "constant": False,
                    "inputs": [
                        {"name": "_to", "type": "address"},
                        {"name": "_value", "type": "uint256"}
                    ],
                    "name": "transfer",
                    "outputs": [{"name": "", "type": "bool"}],
                    "type": "function"
                }
            ]

            try:
                contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(self.gooddollar_contract),
                    abi=erc20_abi
                )
            except Exception as contract_error:
                logger.error(f"❌ Failed to instantiate GoodDollar contract: {contract_error}")
                return {"success": False, "error": "Contract instantiation error"}

            amount_wei = int(amount * (10 ** 18))

            try:
                nonce = self.w3.eth.get_transaction_count(task_account.address)
                gas_price = int(self.w3.eth.gas_price * 1.2)
            except Exception as network_error:
                logger.error(f"❌ Failed to get network info: {network_error}")
                return {"success": False, "error": "Network error"}

            # Estimate gas dynamically instead of hardcoding a fixed limit.
            # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
            # safety buffer on top of the estimate, and fall back to a
            # conservative ceiling only if estimation fails.
            try:
                estimated_gas = contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    amount_wei
                ).estimate_gas({'from': task_account.address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"⛽ Discourse Task gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"⚠️ Gas estimation failed, falling back to 250000: {estimate_error}"
                )
                gas_limit = 250000

            try:
                transfer_txn = contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    amount_wei
                ).build_transaction({
                    'chainId': self.chain_id,
                    'gas': gas_limit,
                    'gasPrice': gas_price,
                    'nonce': nonce,
                    'from': task_account.address
                })
            except Exception as build_error:
                logger.error(f"❌ Failed to build transaction: {build_error}")
                return {"success": False, "error": "Transaction build error"}

            try:
                signed_txn = self.w3.eth.account.sign_transaction(transfer_txn, self.task_key)
            except Exception as sign_error:
                logger.error(f"❌ Failed to sign transaction: {sign_error}")
                return {"success": False, "error": "Transaction signing error"}

            try:
                tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
                tx_hash_hex = tx_hash.hex()
                if not tx_hash_hex.startswith('0x'):
                    tx_hash_hex = '0x' + tx_hash_hex
                logger.info(f"🔗 Discourse Task transaction sent: {tx_hash_hex}")
            except Exception as send_error:
                logger.error(f"❌ Failed to send transaction: {send_error}")
                return {"success": False, "error": "Transaction send error"}

            try:
                logger.info(f"⏳ Waiting for confirmation: {tx_hash_hex}")
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            except Exception as receipt_error:
                logger.error(f"❌ Receipt fetch error: {receipt_error}")
                return {
                    "success": False,
                    "error": "Receipt fetch error",
                    "tx_hash": tx_hash_hex,
                    "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                }

            if receipt.status == 1:
                logger.info(f"✅ Discourse Task reward sent to {masked_wallet}. TX: {tx_hash_hex}")
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                }
            else:
                logger.error(f"❌ Discourse Task transaction failed on-chain. TX: {tx_hash_hex}")
                return {
                    "success": False,
                    "error": "Transaction failed on-chain",
                    "tx_hash": tx_hash_hex,
                    "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                }

        except Exception as e:
            logger.error(f"❌ Discourse Task disbursement error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def disburse_discourse_reward_sync(self, wallet_address: str, amount: float) -> dict:
        """Synchronous wrapper"""
        import concurrent.futures
        try:
            try:
                loop = asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(self._run_in_new_loop, wallet_address, amount)
                    return future.result()
            except RuntimeError:
                return asyncio.run(self.disburse_discourse_reward(wallet_address, amount))
        except Exception as e:
            logger.error(f"❌ Sync wrapper error: {e}")
            return {"success": False, "error": str(e)}

    def _run_in_new_loop(self, wallet_address: str, amount: float) -> dict:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.disburse_discourse_reward(wallet_address, amount))
        finally:
            loop.close()

# Global instance
discourse_blockchain_service = DiscourseTaskBlockchain()
