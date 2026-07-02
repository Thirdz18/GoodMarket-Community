
from env_utils import get_env_float, get_env_int
import os
import logging
from web3 import Web3
from eth_account import Account
from config import GOODDOLLAR_CONTRACT_ADDRESS as _CONFIG_GOODDOLLAR_ADDRESS

logger = logging.getLogger(__name__)

# Minimal G$ ERC-20 ABI used for the DAILYTASK_KEY direct-transfer disbursement.
# G$ is technically ERC-777 on Celo, but the ERC-20 transfer/balanceOf surface
# is the only thing we need for direct payouts (matches the pattern used in
# community_stories/blockchain.py).
_GD_ERC20_ABI = [
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function",
    },
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    },
]


class TwitterTaskBlockchain:
    """Twitter Task Disbursement via direct DAILYTASK_KEY G$ transfer."""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = get_env_int('CHAIN_ID', 42220)
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("✅ Connected to Celo network for Twitter Task")
        else:
            logger.error("❌ Failed to connect to Celo network")

        logger.info("🐦 Twitter Task Blockchain Service initialized (DAILYTASK_KEY direct-transfer mode)")

    def mask_wallet_address(self, wallet_address: str) -> str:
        """Mask wallet address for logging"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def disburse_twitter_reward(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """
        Disburse Twitter Task reward as a direct G$ ERC-20 transfer signed by
        DAILYTASK_KEY. No on-chain reward contract is involved.

        Args:
            wallet_address: Recipient wallet address
            amount: Amount in G$ to send
            task_id: Unique task/submission ID (used only for logging)

        Returns:
            dict: Result with success status, tx_hash, or error
        """
        try:
            masked_wallet = self.mask_wallet_address(wallet_address)
            logger.info(f"🐦 Twitter reward disbursement: {amount} G$ to {masked_wallet} | task_id={task_id}")

            dailytask_key = os.getenv('DAILYTASK_KEY')
            if not dailytask_key:
                logger.error("❌ DAILYTASK_KEY not configured")
                return {"success": False, "error": "DAILYTASK_KEY not configured"}

            if not self.w3.is_connected():
                logger.error("❌ Not connected to Celo network")
                return {"success": False, "error": "Blockchain connection failed"}

            try:
                if not dailytask_key.startswith('0x'):
                    dailytask_key = '0x' + dailytask_key
                dailytask_account = Account.from_key(dailytask_key)
            except Exception as key_error:
                logger.error(f"❌ DAILYTASK_KEY is invalid: {key_error}")
                return {"success": False, "error": "DAILYTASK_KEY invalid"}

            gd_address = _CONFIG_GOODDOLLAR_ADDRESS
            if not gd_address:
                logger.error("❌ GOODDOLLAR_CONTRACT_ADDRESS not configured")
                return {"success": False, "error": "G$ token address not configured"}

            try:
                gd_contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(gd_address),
                    abi=_GD_ERC20_ABI,
                )
            except Exception as contract_error:
                logger.error(f"❌ Failed to load G$ token contract: {contract_error}")
                return {"success": False, "error": "Failed to load G$ token contract"}

            reward_amount_wei = int(round(float(amount) * (10 ** 18)))

            # Pre-flight: verify the sender wallet has enough CELO for gas and
            # enough G$ to cover the transfer. Surfacing this clearly helps ops
            # respond quickly when the wallet needs a top-up.
            try:
                celo_balance = self.w3.eth.get_balance(dailytask_account.address)
                min_celo_required_wei = int(0.005 * (10 ** 18))  # 0.005 CELO floor
                if celo_balance < min_celo_required_wei:
                    logger.error(
                        f"❌ DAILYTASK_KEY wallet has insufficient CELO for gas: "
                        f"{celo_balance / 10**18} CELO. Please top up {dailytask_account.address}."
                    )
                    return {
                        "success": False,
                        "error": "DAILYTASK_KEY wallet needs CELO for gas",
                        "error_type": "insufficient_gas",
                    }
            except Exception as gas_check_err:
                logger.error(f"❌ Failed to check DAILYTASK_KEY wallet CELO balance: {gas_check_err}")
                return {
                    "success": False,
                    "error": "Failed to check DAILYTASK_KEY wallet gas",
                    "error_type": "gas_check_failed",
                }

            try:
                gd_balance = gd_contract.functions.balanceOf(dailytask_account.address).call()
                if gd_balance < reward_amount_wei:
                    logger.error(
                        f"❌ DAILYTASK_KEY wallet has insufficient G$: "
                        f"{gd_balance / 10**18} G$ < {reward_amount_wei / 10**18} G$. "
                        f"Please top up {dailytask_account.address}."
                    )
                    return {
                        "success": False,
                        "error": "DAILYTASK_KEY wallet has insufficient G$",
                        "error_type": "insufficient_balance",
                    }
            except Exception as balance_error:
                logger.error(f"❌ Failed to read DAILYTASK_KEY wallet G$ balance: {balance_error}")
                return {
                    "success": False,
                    "error": "Failed to read DAILYTASK_KEY wallet G$ balance",
                    "error_type": "balance_check_failed",
                }

            try:
                nonce = self.w3.eth.get_transaction_count(dailytask_account.address)
                gas_price = int(self.w3.eth.gas_price * 1.2)
                # Estimate gas dynamically instead of hardcoding a fixed limit.
                # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
                # safety buffer on top of the estimate, and fall back to a
                # conservative ceiling only if estimation fails.
                try:
                    estimated_gas = gd_contract.functions.transfer(
                        Web3.to_checksum_address(wallet_address),
                        int(reward_amount_wei),
                    ).estimate_gas({'from': dailytask_account.address})
                    gas_limit = int(estimated_gas * 1.3)
                    logger.info(
                        f"⛽ Twitter reward gas estimate: {estimated_gas} "
                        f"(using limit: {gas_limit})"
                    )
                except Exception as estimate_error:
                    logger.warning(
                        f"⚠️ Gas estimation failed, falling back to 250000: {estimate_error}"
                    )
                    gas_limit = 250000
                tx = gd_contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    int(reward_amount_wei),
                ).build_transaction({
                    'chainId': self.chain_id,
                    'gas': gas_limit,
                    'gasPrice': gas_price,
                    'nonce': nonce,
                    'from': dailytask_account.address,
                })
            except Exception as build_error:
                logger.error(f"❌ Failed to build transfer tx: {build_error}")
                return {"success": False, "error": "Failed to build transaction"}

            try:
                signed_tx = self.w3.eth.account.sign_transaction(tx, dailytask_key)
                tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
                tx_hash_hex = tx_hash.hex()
                if not tx_hash_hex.startswith('0x'):
                    tx_hash_hex = '0x' + tx_hash_hex
                logger.info(f"📤 Twitter reward transfer sent: {tx_hash_hex}")
            except Exception as send_error:
                logger.error(f"❌ Failed to send transfer tx: {send_error}")
                return {"success": False, "error": f"Failed to send transaction: {str(send_error)}"}

            try:
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            except Exception as receipt_error:
                logger.error(f"❌ Transfer receipt timeout: {receipt_error}")
                return {
                    "success": False,
                    "error": "Transaction timeout",
                    "tx_hash": tx_hash_hex,
                }

            if receipt.status == 1:
                logger.info(
                    f"✅ Twitter reward disbursed: {reward_amount_wei / 10**18} G$ "
                    f"to {masked_wallet} | tx={tx_hash_hex}"
                )
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": reward_amount_wei / 10**18,
                    "recipient": wallet_address,
                    "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
                }

            logger.error(f"❌ Twitter reward transfer reverted on-chain | tx={tx_hash_hex}")
            return {
                "success": False,
                "error": "Transfer reverted on-chain",
                "error_type": "reverted",
                "tx_hash": tx_hash_hex,
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
            }

        except Exception as e:
            logger.error(f"❌ Twitter reward disbursement error: {e}")
            import traceback
            logger.error(f"🔍 Traceback: {traceback.format_exc()}")
            return {"success": False, "error": str(e)}

    def disburse_twitter_reward_sync(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Synchronous wrapper for async disbursement"""
        import asyncio
        import concurrent.futures

        try:
            loop = asyncio.get_running_loop()
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(self._run_in_new_loop, wallet_address, amount, task_id)
                return future.result()
        except RuntimeError:
            return asyncio.run(self.disburse_twitter_reward(wallet_address, amount, task_id))
        except Exception as e:
            logger.error(f"❌ Sync disbursement wrapper error: {e}")
            return {"success": False, "error": str(e)}

    def _run_in_new_loop(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Helper to run async function in a new loop in a separate thread"""
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.disburse_twitter_reward(wallet_address, amount, task_id))
        finally:
            loop.close()


# Global instance
twitter_blockchain_service = TwitterTaskBlockchain()
