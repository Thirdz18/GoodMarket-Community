import os
import asyncio
import logging
import time
import uuid
from decimal import Decimal, ROUND_DOWN
from datetime import datetime
from web3 import Web3
from eth_account import Account
from web3.exceptions import TimeExhausted
from config import LEARN_EARN_CONTRACT_ADDRESS as _CONFIG_LEARN_EARN_ADDRESS

logger = logging.getLogger(__name__)


class LearnBlockchainService:
    """Learn & Earn Smart Contract Disbursement Service
    
    Uses the deployed LearnAndEarnRewards smart contract for secure G$ disbursements.
    Falls back to direct transfer only if contract is not configured.
    """

    MAX_RETRIES = 3
    RETRY_DELAY_BASE = 2

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.gooddollar_address = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
        self.contract_address = _CONFIG_LEARN_EARN_ADDRESS or None
        self._wallet_key = os.getenv('LEARN_WALLET_PRIVATE_KEY')
        self.tx_receipt_timeout = int(os.getenv('TX_RECEIPT_TIMEOUT', '300'))

        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url, request_kwargs={'timeout': 30}))
        self.contract = None
        self.owner_account = None

        if self.w3.is_connected():
            logger.info("Connected to Celo network for Learn & Earn")
        else:
            logger.error("Failed to connect to Celo network")

        self._initialize()

    def _initialize(self):
        """Initialize contract and wallet"""
        try:
            if self.contract_address:
                self.contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(self.contract_address),
                    abi=self._get_contract_abi()
                )
                logger.info(f"Learn & Earn Contract loaded: {self.contract_address[:10]}...")
            else:
                logger.warning("Learn & Earn contract not configured")

            if self._wallet_key:
                key = self._wallet_key if self._wallet_key.startswith('0x') else '0x' + self._wallet_key
                self.owner_account = Account.from_key(key)
                logger.info("Learn & Earn wallet configured")
            else:
                logger.warning("Learn & Earn wallet not configured")

        except Exception as e:
            logger.error(f"Initialization error: {type(e).__name__}")

    @property
    def is_configured(self) -> bool:
        """Check if the service is properly configured (without exposing private key)"""
        return self.owner_account is not None

    def _get_contract_abi(self):
        """Get minimal ABI for contract interactions"""
        return [
            {"inputs": [], "name": "getContractBalance", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [{"name": "recipient", "type": "address"}, {"name": "amount", "type": "uint256"}, {"name": "quizId", "type": "string"}], "name": "disburseReward", "outputs": [{"type": "bytes32"}], "stateMutability": "nonpayable", "type": "function"},
            {"inputs": [{"name": "recipient", "type": "address"}, {"name": "quizId", "type": "string"}], "name": "isQuizRewardClaimed", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "paused", "outputs": [{"type": "bool"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "maxDisbursementAmount", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
            {"inputs": [], "name": "minDisbursementAmount", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
        ]

    def _get_erc20_abi(self):
        """Get ERC20 ABI for balance checks"""
        return [
            {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
        ]

    def _generate_quiz_id(self, wallet_address: str, quiz_result_summary: dict = None) -> str:
        """Generate a unique, deterministic quiz ID using wallet + timestamp + uuid"""
        timestamp = int(datetime.now().timestamp())
        unique_part = uuid.uuid4().hex[:8]
        short_wallet = wallet_address[-8:].lower()
        return f"quiz_{short_wallet}_{timestamp}_{unique_part}"

    def _safe_amount_wei(self, amount: float) -> int:
        """Convert G$ amount to wei safely, avoiding floating point precision issues.
        
        Uses Decimal for precise conversion to prevent amounts like 1000.0000000000001
        from exceeding the contract's maxDisbursementAmount.
        """
        d_amount = Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        amount_wei = int(d_amount * Decimal('1000000000000000000'))

        try:
            if self.contract:
                max_wei = self.contract.functions.maxDisbursementAmount().call()
                min_wei = self.contract.functions.minDisbursementAmount().call()

                if amount_wei > max_wei:
                    logger.warning(f"Amount {amount_wei} exceeds max {max_wei}, capping to max")
                    amount_wei = max_wei
                elif amount_wei < min_wei:
                    logger.warning(f"Amount {amount_wei} below min {min_wei}, raising to min")
                    amount_wei = min_wei
        except Exception as e:
            logger.warning(f"Could not check disbursement limits: {e}")

        return amount_wei

    async def get_contract_balance(self) -> float:
        """Get the G$ balance of the Learn & Earn contract"""
        try:
            if not self.contract:
                logger.error("Contract not configured")
                return 0.0

            balance_wei = self.contract.functions.getContractBalance().call()
            balance_g = balance_wei / (10 ** 18)
            logger.info(f"Contract balance: {balance_g:.2f} G$")
            return balance_g

        except Exception as e:
            logger.error(f"Error getting contract balance: {type(e).__name__}: {e}")
            return 0.0

    async def get_learn_wallet_balance(self) -> float:
        """Get the G$ balance of the Learn wallet (for legacy compatibility)"""
        try:
            if self.contract:
                return await self.get_contract_balance()

            if not self.owner_account:
                return 0.0

            erc20 = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_address),
                abi=self._get_erc20_abi()
            )
            balance_wei = erc20.functions.balanceOf(self.owner_account.address).call()
            return balance_wei / (10 ** 18)

        except Exception as e:
            logger.error(f"Error getting balance: {type(e).__name__}: {e}")
            return 0.0

    async def send_g_reward(self, wallet_address: str, amount: float, quiz_result_summary: dict = None) -> dict:
        """Send G$ rewards - uses smart contract with unique quiz ID"""
        try:
            quiz_id = self._generate_quiz_id(wallet_address, quiz_result_summary)
            logger.info(f"Generated unique quiz_id: {quiz_id}")
            return await self.disburse_quiz_reward(wallet_address, amount, quiz_id)

        except Exception as e:
            logger.error(f"Error sending reward: {type(e).__name__}: {e}")
            return {"success": False, "error": "Failed to send reward"}

    async def disburse_quiz_reward(self, wallet_address: str, amount: float, quiz_id: str) -> dict:
        """Send G$ rewards via smart contract with retry logic"""
        last_error = None

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                result = await self._attempt_disburse(wallet_address, amount, quiz_id, attempt)

                if result.get('success'):
                    return result

                if result.get('permanent_failure'):
                    return result

                last_error = result.get('error', 'Unknown error')
                logger.warning(f"Attempt {attempt}/{self.MAX_RETRIES} failed: {last_error}")

                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_DELAY_BASE ** attempt
                    logger.info(f"Retrying in {delay}s...")
                    await asyncio.sleep(delay)

            except Exception as e:
                last_error = str(e)
                logger.error(f"Attempt {attempt}/{self.MAX_RETRIES} exception: {type(e).__name__}: {e}")

                if attempt < self.MAX_RETRIES:
                    delay = self.RETRY_DELAY_BASE ** attempt
                    await asyncio.sleep(delay)

        error_msg = self._sanitize_error(last_error or "Failed after all retries")
        return {"success": False, "error": error_msg}

    async def _attempt_direct_transfer(self, wallet_address: str, amount: float, attempt: int) -> dict:
        """Direct ERC20 transfer fallback when smart contract is not configured"""
        logger.info(f"Direct ERC20 transfer attempt {attempt}: {amount} G$ to {wallet_address[:10]}...")
        try:
            erc20_abi = [
                {"inputs": [{"name": "recipient", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "transfer", "outputs": [{"type": "bool"}], "stateMutability": "nonpayable", "type": "function"},
                {"inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"type": "uint256"}], "stateMutability": "view", "type": "function"},
            ]
            token = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_address),
                abi=erc20_abi
            )

            amount_wei = int(Decimal(str(amount)).quantize(Decimal('0.01'), rounding=ROUND_DOWN) * Decimal('1000000000000000000'))

            balance_wei = token.functions.balanceOf(self.owner_account.address).call()
            balance_g = balance_wei / (10 ** 18)
            logger.info(f"Learn wallet balance: {balance_g:.4f} G$, need: {amount} G$")

            if balance_wei < amount_wei:
                return {"success": False, "error": "Rewards pool is currently depleted. Please try again later.", "permanent_failure": True}

            nonce = self.w3.eth.get_transaction_count(self.owner_account.address, 'pending')
            gas_price = int(self.w3.eth.gas_price * 1.2)
            if attempt > 1:
                gas_price = int(gas_price * (1 + (attempt * 0.1)))

            try:
                estimated_gas = token.functions.transfer(
                    Web3.to_checksum_address(wallet_address), amount_wei
                ).estimate_gas({'from': self.owner_account.address})
                gas_limit = int(estimated_gas * 1.3)
            except Exception:
                gas_limit = 100000

            txn = token.functions.transfer(
                Web3.to_checksum_address(wallet_address), amount_wei
            ).build_transaction({
                'chainId': self.chain_id,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
            })

            signed_txn = self.w3.eth.account.sign_transaction(txn, self._wallet_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            logger.info(f"Direct transfer sent: {tx_hash_hex}")
            receipt = self._wait_for_receipt(tx_hash)

            if receipt.status == 1:
                logger.info(f"Direct transfer success: {amount} G$ → {wallet_address[:10]} TX: {tx_hash_hex}")
                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
                    "gas_used": receipt.gasUsed,
                    "block_number": receipt.blockNumber,
                    "method": "direct_transfer"
                }
            else:
                logger.error(f"Direct transfer REVERTED: {tx_hash_hex}")
                return {"success": False, "error": "Transaction reverted. Please try again."}

        except Exception as e:
            logger.error(f"Direct transfer error: {type(e).__name__}: {e}")
            return {"success": False, "error": self._sanitize_error(str(e))}

    async def _attempt_disburse(self, wallet_address: str, amount: float, quiz_id: str, attempt: int) -> dict:
        """Single attempt to disburse reward"""
        logger.info(f"Quiz reward attempt {attempt}: {amount} G$ to {wallet_address[:10]}...")

        if not self.owner_account:
            return {"success": False, "error": "Reward system not configured. Please contact support.", "permanent_failure": True}

        if not self._wallet_key:
            return {"success": False, "error": "Reward system not configured. Please contact support.", "permanent_failure": True}

        if not self.contract:
            logger.warning("Smart contract not configured — using direct ERC20 transfer fallback")
            return await self._attempt_direct_transfer(wallet_address, amount, attempt)

        try:
            is_paused = self.contract.functions.paused().call()
            if is_paused:
                return {"success": False, "error": "Reward system is temporarily paused. Please try again later.", "permanent_failure": True}
        except Exception as e:
            logger.warning(f"Paused check failed: {e}")

        balance = await self.get_contract_balance()
        if balance < amount:
            logger.warning(f"Insufficient contract balance: {balance:.2f} G$ < {amount} G$ — falling back to direct ERC20 transfer")
            return await self._attempt_direct_transfer(wallet_address, amount, attempt)

        try:
            already_claimed = self.contract.functions.isQuizRewardClaimed(
                Web3.to_checksum_address(wallet_address),
                quiz_id
            ).call()
            if already_claimed:
                return {"success": False, "error": "Reward already claimed for this quiz.", "permanent_failure": True}
        except Exception as e:
            logger.warning(f"Claim check failed: {e}")

        amount_wei = self._safe_amount_wei(amount)
        logger.info(f"Amount: {amount} G$ = {amount_wei} wei (safe conversion)")

        nonce = self.w3.eth.get_transaction_count(self.owner_account.address, 'pending')
        gas_price = int(self.w3.eth.gas_price * 1.2)

        if attempt > 1:
            gas_price = int(gas_price * (1 + (attempt * 0.1)))
            logger.info(f"Bumped gas price for retry attempt {attempt}")

        try:
            estimated_gas = self.contract.functions.disburseReward(
                Web3.to_checksum_address(wallet_address),
                amount_wei,
                quiz_id
            ).estimate_gas({'from': self.owner_account.address})
            gas_limit = int(estimated_gas * 1.3)
            logger.info(f"Estimated gas: {estimated_gas}, using limit: {gas_limit}")
        except Exception as gas_err:
            logger.warning(f"Gas estimation failed ({gas_err}), using default 500000")
            gas_limit = 500000

        txn = self.contract.functions.disburseReward(
            Web3.to_checksum_address(wallet_address),
            amount_wei,
            quiz_id
        ).build_transaction({
            'chainId': self.chain_id,
            'gas': gas_limit,
            'gasPrice': gas_price,
            'nonce': nonce,
        })

        signed_txn = self.w3.eth.account.sign_transaction(txn, self._wallet_key)

        logger.info(f"Sending reward transaction (nonce={nonce}, gas={gas_limit}, gasPrice={gas_price})...")
        tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
        tx_hash_hex = tx_hash.hex()
        if not tx_hash_hex.startswith('0x'):
            tx_hash_hex = '0x' + tx_hash_hex

        logger.info(f"Transaction sent: {tx_hash_hex}")

        receipt = self._wait_for_receipt(tx_hash)

        if receipt.status == 1:
            logger.info(f"Reward sent successfully: {amount} G$ - TX: {tx_hash_hex} - Block: {receipt.blockNumber} - Gas: {receipt.gasUsed}")
            return {
                "success": True,
                "tx_hash": tx_hash_hex,
                "amount": amount,
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}",
                "gas_used": receipt.gasUsed,
                "block_number": receipt.blockNumber
            }
        else:
            revert_reason = "Unknown"
            try:
                self.w3.eth.call({
                    'to': txn['to'],
                    'from': self.owner_account.address,
                    'data': txn['data'],
                    'value': txn.get('value', 0),
                }, receipt.blockNumber
                )
            except Exception as revert_err:
                revert_reason = str(revert_err)

            logger.error(f"Transaction REVERTED: {tx_hash_hex} - Block: {receipt.blockNumber} - Gas: {receipt.gasUsed}")
            logger.error(f"Revert reason: {revert_reason}")
            logger.error(f"Revert details - Wallet: {wallet_address[:10]}, Amount: {amount} G$ ({amount_wei} wei), QuizID: {quiz_id}")
            return {
                "success": False,
                "error": f"Transaction reverted: {revert_reason}",
                "tx_hash": tx_hash_hex,
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}"
            }

    def _wait_for_receipt(self, tx_hash):
        """
        Wait for transaction receipt with configurable timeout and manual fallback polling.
        Prevents frequent 120s false-failure during temporary Celo congestion.
        """
        try:
            return self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=self.tx_receipt_timeout)
        except TimeExhausted:
            tx_hex = tx_hash.hex() if hasattr(tx_hash, 'hex') else str(tx_hash)
            logger.warning(
                f"Receipt timeout after {self.tx_receipt_timeout}s for tx {tx_hex}. "
                "Polling manually for final status..."
            )
            manual_poll_seconds = 60
            deadline = time.time() + manual_poll_seconds
            while time.time() < deadline:
                try:
                    receipt = self.w3.eth.get_transaction_receipt(tx_hash)
                    if receipt:
                        return receipt
                except Exception:
                    pass
                time.sleep(3)
            raise TimeExhausted(
                f"Transaction not mined after {self.tx_receipt_timeout + manual_poll_seconds}s total wait"
            )

    def _sanitize_error(self, error_msg: str) -> str:
        """Remove sensitive info from error messages shown to users"""
        error_lower = error_msg.lower()
        if 'private' in error_lower or 'key' in error_lower:
            return "Configuration error"
        elif 'insufficient' in error_lower:
            return "Rewards pool is currently depleted"
        elif 'nonce' in error_lower:
            return "Transaction conflict, please try again."
        elif 'already processed' in error_lower or 'already claimed' in error_lower:
            return "Reward already claimed for this quiz."
        elif 'timeout' in error_lower or 'timed out' in error_lower:
            return "Network timeout. Please try again."
        elif 'revert' in error_lower or 'execution reverted' in error_lower:
            return "Transaction was rejected by the contract. Please try again."
        else:
            return "Failed to process reward. Please try again."


learn_blockchain_service = LearnBlockchainService()


def disburse_rewards(wallet_address, amount, score):
    """Legacy function for backward compatibility"""
    import asyncio
    quiz_id = learn_blockchain_service._generate_quiz_id(wallet_address)
    try:
        loop = asyncio.get_event_loop()
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    
    return loop.run_until_complete(
        learn_blockchain_service.disburse_quiz_reward(wallet_address, amount, quiz_id)
    )
