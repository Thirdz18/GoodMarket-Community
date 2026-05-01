
import os
import asyncio
import logging
from web3 import Web3
from eth_account import Account
from config import (
    DAILY_TASK_CONTRACT_ADDRESS as _CONFIG_DAILY_TASK_ADDRESS,
    GOODDOLLAR_CONTRACT_ADDRESS as _CONFIG_GOODDOLLAR_ADDRESS,
)

logger = logging.getLogger(__name__)

# Minimal G$ ERC-20 ABI used for the DAILYTASK_KEY direct-transfer fallback.
# G$ is technically ERC-777 on Celo, but the ERC-20 transfer/balanceOf surface
# is the only thing we need for direct payouts (matches the pattern used in
# community_stories/blockchain.py).
_GD_ERC20_FALLBACK_ABI = [
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

def _decode_revert_reason(data: bytes) -> str:
    """Decode revert reason from raw bytes returned by eth_call"""
    try:
        if not data or data == b'':
            return "No revert reason returned"
        if data[:4] == bytes.fromhex('08c379a0'):
            reason = data[4:]
            length = int.from_bytes(reason[32:64], 'big')
            return reason[64:64 + length].decode('utf-8', errors='replace')
        if data[:4] == bytes.fromhex('4e487b71'):
            code = int.from_bytes(data[4:], 'big')
            return f"Panic code {code}"
        return f"Unknown revert data: {data.hex()[:64]}"
    except Exception as e:
        return f"Could not decode revert: {str(e)}"

DAILY_TASK_CONTRACT_ABI = [
    {
        "inputs": [
            {"internalType": "address", "name": "recipient", "type": "address"},
            {"internalType": "string", "name": "taskId", "type": "string"},
            {"internalType": "string", "name": "platform", "type": "string"}
        ],
        "name": "disburseReward",
        "outputs": [{"internalType": "bytes32", "name": "", "type": "bytes32"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getContractBalance",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "rewardAmount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]

class TelegramTaskBlockchain:
    """Telegram Task Disbursement via DailyTaskRewards Contract"""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.daily_task_contract_address = _CONFIG_DAILY_TASK_ADDRESS

        self.task_key = os.getenv('TASK_KEY')

        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("✅ Connected to Celo network for Telegram Task")
        else:
            logger.error("❌ Failed to connect to Celo network")

        if self.daily_task_contract_address:
            logger.info(f"📋 DailyTaskRewards contract: {self.daily_task_contract_address}")
        else:
            logger.error("❌ DAILY_TASK_CONTRACT_ADDRESS not set")

        logger.info("📱 Telegram Task Blockchain Service initialized (contract mode)")

    def mask_wallet_address(self, wallet_address: str) -> str:
        """Mask wallet address for logging"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def disburse_telegram_reward(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """
        Disburse Telegram Task reward via DailyTaskRewards contract.
        TASK_KEY signs the disburseReward() call on the contract.

        Args:
            wallet_address: Recipient wallet address
            amount: Amount in G$ (informational — actual amount set on contract)
            task_id: Unique task/submission ID for deduplication

        Returns:
            dict: Result with success status, tx_hash, or error
        """
        try:
            masked_wallet = self.mask_wallet_address(wallet_address)
            logger.info(f"📱 Telegram reward disbursement: to {masked_wallet} | task_id={task_id}")

            task_key = os.getenv('TASK_KEY') or self.task_key

            if not task_key:
                logger.error("❌ TASK_KEY not configured")
                return {"success": False, "error": "Task key not configured"}

            if not self.daily_task_contract_address:
                logger.error("❌ DAILY_TASK_CONTRACT_ADDRESS not configured")
                return {"success": False, "error": "Daily task contract address not configured"}

            if not task_id:
                logger.error("❌ task_id is required for contract disbursement")
                return {"success": False, "error": "task_id is required"}

            if not self.w3.is_connected():
                logger.error("❌ Not connected to Celo network")
                return {"success": False, "error": "Blockchain connection failed"}

            try:
                if task_key.startswith('0x'):
                    task_account = Account.from_key(task_key)
                else:
                    task_account = Account.from_key('0x' + task_key)
                logger.info(f"🔑 Task account: {self.mask_wallet_address(task_account.address)}")
            except Exception as key_error:
                logger.error(f"❌ Failed to load TASK_KEY: {key_error}")
                return {"success": False, "error": "Key loading error"}

            try:
                contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(self.daily_task_contract_address),
                    abi=DAILY_TASK_CONTRACT_ABI
                )
            except Exception as contract_error:
                logger.error(f"❌ Failed to load DailyTaskRewards contract: {contract_error}")
                return {"success": False, "error": "Contract load error"}

            try:
                contract_balance = contract.functions.getContractBalance().call()
                reward_amount = contract.functions.rewardAmount().call()
                logger.info(f"💵 Contract balance: {contract_balance / 10**18} G$ | Reward: {reward_amount / 10**18} G$")

                if contract_balance < reward_amount:
                    logger.warning(
                        f"⚠️ DailyTaskRewards contract is short on G$: "
                        f"{contract_balance / 10**18} G$ < {reward_amount / 10**18} G$. "
                        f"Attempting DAILYTASK_KEY fallback (direct G$ transfer)."
                    )
                    fallback_result = self._disburse_via_fallback_key(
                        wallet_address=wallet_address,
                        reward_amount_wei=reward_amount,
                        task_id=task_id,
                    )
                    # Only short-circuit if the fallback actually ran. If it's
                    # not configured, fall back to the original error so the
                    # caller's behavior is unchanged.
                    if fallback_result is not None:
                        return fallback_result

                    logger.error(
                        f"❌ Insufficient contract balance and DAILYTASK_KEY fallback unavailable: "
                        f"{contract_balance / 10**18} G$ < {reward_amount / 10**18} G$"
                    )
                    return {
                        "success": False,
                        "error": "insufficient_balance",
                        "error_type": "insufficient_balance",
                        "message": "The DailyTaskRewards contract needs to be funded. Please deposit G$ to the contract."
                    }
            except Exception as balance_error:
                logger.error(f"❌ Failed to check contract balance: {balance_error}")
                return {"success": False, "error": "Failed to check contract balance"}

            try:
                nonce = self.w3.eth.get_transaction_count(task_account.address)
                gas_price = int(self.w3.eth.gas_price * 1.2)
                logger.info(f"⛽ Gas price: {gas_price} wei")
            except Exception as network_error:
                logger.error(f"❌ Failed to get network info: {network_error}")
                return {"success": False, "error": "Network error"}

            try:
                txn = contract.functions.disburseReward(
                    Web3.to_checksum_address(wallet_address),
                    str(task_id),
                    "telegram"
                ).build_transaction({
                    'chainId': self.chain_id,
                    'gas': 600000,
                    'gasPrice': gas_price,
                    'nonce': nonce,
                    'from': task_account.address
                })
            except Exception as build_error:
                logger.error(f"❌ Failed to build transaction: {build_error}")
                return {"success": False, "error": "Transaction build error"}

            try:
                signed_txn = self.w3.eth.account.sign_transaction(txn, task_key)
            except Exception as sign_error:
                logger.error(f"❌ Failed to sign transaction: {sign_error}")
                return {"success": False, "error": "Transaction signing error"}

            try:
                tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
                tx_hash_hex = tx_hash.hex()
                if not tx_hash_hex.startswith('0x'):
                    tx_hash_hex = '0x' + tx_hash_hex
                logger.info(f"🔗 Telegram Task transaction sent: {tx_hash_hex}")
            except Exception as send_error:
                logger.error(f"❌ Failed to send transaction: {send_error}")
                return {"success": False, "error": "Transaction send error"}

            try:
                logger.info(f"⏳ Waiting for confirmation: {tx_hash_hex}")
                receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            except Exception as receipt_error:
                logger.error(f"❌ Error fetching receipt: {receipt_error}")
                return {
                    "success": False,
                    "error": "Receipt fetch error",
                    "tx_hash": tx_hash_hex,
                    "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}"
                }

            if receipt.status == 1:
                logger.info(f"✅ Telegram reward disbursed via contract to {masked_wallet}. TX: {tx_hash_hex}")
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": reward_amount / 10**18,
                    "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
                    "contract": self.daily_task_contract_address
                }
            else:
                # Try to decode the exact revert reason via eth_call simulation
                revert_reason = "Unknown"
                try:
                    call_data = contract.functions.disburseReward(
                        Web3.to_checksum_address(wallet_address),
                        str(task_id),
                        "telegram"
                    ).build_transaction({
                        'chainId': self.chain_id,
                        'gas': 600000,
                        'gasPrice': gas_price,
                        'nonce': nonce,
                        'from': task_account.address
                    })
                    self.w3.eth.call(call_data, receipt.blockNumber)
                except Exception as call_err:
                    err_str = str(call_err)
                    if hasattr(call_err, 'data') and call_err.data:
                        raw = call_err.data
                        if isinstance(raw, str):
                            raw = bytes.fromhex(raw.replace('0x', ''))
                        revert_reason = _decode_revert_reason(raw)
                    else:
                        revert_reason = err_str

                # Classify the reason
                reason_lower = revert_reason.lower()
                if any(k in reason_lower for k in ['already', 'duplicate', 'rewarded', 'claimed']):
                    error_type = "already_rewarded"
                    friendly = f"Already rewarded: {revert_reason}"
                elif any(k in reason_lower for k in ['balance', 'insufficient', 'funds']):
                    error_type = "insufficient_balance"
                    friendly = f"Insufficient contract balance: {revert_reason}"
                elif any(k in reason_lower for k in ['access', 'owner', 'authorized', 'permission']):
                    error_type = "access_denied"
                    friendly = f"Access denied: {revert_reason}"
                else:
                    error_type = "contract_revert"
                    friendly = f"Contract reverted: {revert_reason}"

                logger.error(f"❌ Telegram transaction failed on-chain [{error_type}]: {revert_reason} | TX: {tx_hash_hex}")
                return {
                    "success": False,
                    "error": friendly,
                    "error_type": error_type,
                    "revert_reason": revert_reason,
                    "tx_hash": tx_hash_hex,
                    "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}"
                }

        except Exception as e:
            logger.error(f"❌ Telegram Task reward disbursement error: {e}", exc_info=True)
            return {"success": False, "error": str(e)}

    def _disburse_via_fallback_key(self, wallet_address: str, reward_amount_wei: int, task_id: str) -> dict:
        """
        Fallback path: when the DailyTaskRewards contract is empty/insufficient,
        use the DAILYTASK_KEY wallet to send G$ directly to the user via a plain
        ERC-20 transfer.

        Returns:
            - dict result (success or failure) when the fallback actually ran
            - None when DAILYTASK_KEY is not configured, so the caller can keep
              the original insufficient_balance error path (backward compatible).
        """
        fallback_key = os.getenv('DAILYTASK_KEY')
        if not fallback_key:
            logger.warning("⚠️ DAILYTASK_KEY not configured — fallback disabled, original error will be returned")
            return None

        try:
            if not fallback_key.startswith('0x'):
                fallback_key = '0x' + fallback_key
            fallback_account = Account.from_key(fallback_key)
        except Exception as key_error:
            logger.error(f"❌ DAILYTASK_KEY is invalid: {key_error}")
            return {
                "success": False,
                "error": "Fallback key invalid",
                "error_type": "fallback_key_invalid",
            }

        masked = self.mask_wallet_address(fallback_account.address)
        logger.warning(f"🚨 FALLBACK TRIGGERED [telegram] — using DAILYTASK_KEY {masked} to direct-transfer G$ to user (task_id={task_id})")

        gd_address = _CONFIG_GOODDOLLAR_ADDRESS
        if not gd_address:
            logger.error("❌ GOODDOLLAR_CONTRACT_ADDRESS not configured — cannot run fallback")
            return {
                "success": False,
                "error": "G$ token address not configured",
                "error_type": "fallback_token_missing",
            }

        try:
            celo_balance = self.w3.eth.get_balance(fallback_account.address)
            min_celo_required_wei = int(0.005 * (10 ** 18))  # 0.005 CELO floor
            if celo_balance < min_celo_required_wei:
                logger.error(
                    f"❌ DAILYTASK_KEY wallet has insufficient CELO for gas: "
                    f"{celo_balance / 10**18} CELO. Please top up {fallback_account.address}."
                )
                return {
                    "success": False,
                    "error": "Fallback wallet needs CELO for gas",
                    "error_type": "fallback_insufficient_gas",
                    "fallback_used": True,
                }
        except Exception as gas_check_err:
            logger.error(f"❌ Failed to check fallback wallet CELO balance: {gas_check_err}")
            return {
                "success": False,
                "error": "Failed to check fallback wallet gas",
                "error_type": "fallback_gas_check_failed",
                "fallback_used": True,
            }

        try:
            gd_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(gd_address),
                abi=_GD_ERC20_FALLBACK_ABI,
            )
        except Exception as contract_error:
            logger.error(f"❌ Failed to load G$ token contract for fallback: {contract_error}")
            return {
                "success": False,
                "error": "Failed to load G$ token contract",
                "error_type": "fallback_contract_load_failed",
            }

        try:
            fallback_gd_balance = gd_contract.functions.balanceOf(fallback_account.address).call()
            if fallback_gd_balance < reward_amount_wei:
                logger.error(
                    f"❌ DAILYTASK_KEY wallet has insufficient G$: "
                    f"{fallback_gd_balance / 10**18} G$ < {reward_amount_wei / 10**18} G$. "
                    f"Please top up {fallback_account.address}."
                )
                return {
                    "success": False,
                    "error": "Fallback wallet has insufficient G$",
                    "error_type": "fallback_insufficient_balance",
                    "fallback_used": True,
                }
        except Exception as balance_error:
            logger.error(f"❌ Failed to read fallback wallet G$ balance: {balance_error}")
            return {
                "success": False,
                "error": "Failed to read fallback wallet G$ balance",
                "error_type": "fallback_balance_check_failed",
                "fallback_used": True,
            }

        try:
            nonce = self.w3.eth.get_transaction_count(fallback_account.address)
            gas_price = int(self.w3.eth.gas_price * 1.2)
            tx = gd_contract.functions.transfer(
                Web3.to_checksum_address(wallet_address),
                int(reward_amount_wei),
            ).build_transaction({
                'chainId': self.chain_id,
                'gas': 250000,  # G$ ERC-777 hooks add overhead vs plain ERC-20
                'gasPrice': gas_price,
                'nonce': nonce,
                'from': fallback_account.address,
            })
        except Exception as build_error:
            logger.error(f"❌ Failed to build fallback transfer tx: {build_error}")
            return {
                "success": False,
                "error": "Failed to build fallback transaction",
                "error_type": "fallback_build_failed",
                "fallback_used": True,
            }

        try:
            signed_tx = self.w3.eth.account.sign_transaction(tx, fallback_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex
            logger.info(f"📤 [fallback] Transfer sent: {tx_hash_hex}")
        except Exception as send_error:
            logger.error(f"❌ Failed to send fallback tx: {send_error}")
            return {
                "success": False,
                "error": f"Failed to send fallback transaction: {str(send_error)}",
                "error_type": "fallback_send_failed",
                "fallback_used": True,
            }

        try:
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        except Exception as receipt_error:
            logger.error(f"❌ Fallback receipt timeout: {receipt_error}")
            return {
                "success": False,
                "error": "Fallback transaction timeout",
                "error_type": "fallback_receipt_timeout",
                "tx_hash": tx_hash_hex,
                "fallback_used": True,
            }

        if receipt.status == 1:
            logger.warning(
                f"✅ FALLBACK SUCCESS [telegram] — DAILYTASK_KEY paid out "
                f"{reward_amount_wei / 10**18} G$ to {self.mask_wallet_address(wallet_address)} | tx={tx_hash_hex}"
            )
            return {
                "success": True,
                "tx_hash": tx_hash_hex,
                "amount": reward_amount_wei / 10**18,
                "recipient": wallet_address,
                "fallback_used": True,
                "fallback_reason": "contract_insufficient_balance",
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
            }

        logger.error(f"❌ Fallback transfer reverted on-chain | tx={tx_hash_hex}")
        return {
            "success": False,
            "error": "Fallback transfer reverted on-chain",
            "error_type": "fallback_reverted",
            "tx_hash": tx_hash_hex,
            "fallback_used": True,
            "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
        }

    def disburse_telegram_reward_sync(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Synchronous wrapper for disburse_telegram_reward"""
        import asyncio
        import concurrent.futures

        try:
            try:
                loop = asyncio.get_running_loop()
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    future = pool.submit(self._run_in_new_loop, wallet_address, amount, task_id)
                    return future.result()
            except RuntimeError:
                return asyncio.run(self.disburse_telegram_reward(wallet_address, amount, task_id))
        except Exception as e:
            logger.error(f"❌ Sync disbursement wrapper error: {e}")
            return {"success": False, "error": str(e)}

    def _run_in_new_loop(self, wallet_address: str, amount: float, task_id: str = None) -> dict:
        """Helper to run async function in a new loop in a separate thread"""
        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.disburse_telegram_reward(wallet_address, amount, task_id))
        finally:
            loop.close()


# Global instance
telegram_blockchain_service = TelegramTaskBlockchain()
