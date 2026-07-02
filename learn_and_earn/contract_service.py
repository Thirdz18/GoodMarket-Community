"""
Learn & Earn Contract Service

This service interacts with the deployed LearnAndEarnRewards smart contract
for G$ token disbursements on the Celo network.

Uses LEARN_WALLET_PRIVATE_KEY as the contract owner to sign all transactions.
"""
from env_utils import get_env_float, get_env_int

import os
import json
import logging
from datetime import datetime
from web3 import Web3
from eth_account import Account
from config import LEARN_EARN_CONTRACT_ADDRESS as _CONFIG_LEARN_EARN_ADDRESS

logger = logging.getLogger(__name__)


CONTRACT_ABI = [
    {
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "deposit",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "from", "type": "address"}, {"name": "amount", "type": "uint256"}],
        "name": "depositFrom",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "quizId", "type": "string"}
        ],
        "name": "disburseReward",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "recipients", "type": "address[]"},
            {"name": "amounts", "type": "uint256[]"},
            {"name": "quizIds", "type": "string[]"}
        ],
        "name": "batchDisburseRewards",
        "outputs": [{"name": "", "type": "bytes32[]"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "amount", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "withdrawAll",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getContractBalance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "user", "type": "address"}],
        "name": "getUserStats",
        "outputs": [
            {"name": "totalRewards", "type": "uint256"},
            {"name": "rewardCount", "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "getContractStats",
        "outputs": [
            {"name": "balance", "type": "uint256"},
            {"name": "deposited", "type": "uint256"},
            {"name": "disbursed", "type": "uint256"},
            {"name": "withdrawn", "type": "uint256"}
        ],
        "stateMutability": "view",
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
        "name": "paused",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "pause",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "unpause",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "maxDisbursementAmount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [],
        "name": "minDisbursementAmount",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "newMax", "type": "uint256"}],
        "name": "setMaxDisbursementAmount",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "newMin", "type": "uint256"}],
        "name": "setMinDisbursementAmount",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"name": "rewardId", "type": "bytes32"}],
        "name": "isRewardProcessed",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "quizId", "type": "string"}
        ],
        "name": "isQuizRewardClaimed",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "recipient", "type": "address"},
            {"name": "quizId", "type": "string"}
        ],
        "name": "getRewardId",
        "outputs": [{"name": "", "type": "bytes32"}],
        "stateMutability": "pure",
        "type": "function"
    }
]

ERC20_APPROVE_ABI = [
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [
            {"name": "owner", "type": "address"},
            {"name": "spender", "type": "address"}
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"name": "account", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function"
    }
]


class LearnEarnContractService:
    """Service for interacting with the Learn & Earn Rewards smart contract"""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = get_env_int('CHAIN_ID', 42220)
        self.gooddollar_address = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
        self.contract_address = _CONFIG_LEARN_EARN_ADDRESS or None
        self.learn_wallet_key = os.getenv('LEARN_WALLET_PRIVATE_KEY')

        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))
        self.contract = None
        self.gooddollar_contract = None
        self.owner_account = None

        self._initialize()

    def _initialize(self):
        """Initialize Web3 connection and contract instances"""
        try:
            if not self.w3.is_connected():
                logger.error("❌ Failed to connect to Celo network")
                return

            logger.info("✅ Connected to Celo network for Learn & Earn Contract")

            if self.contract_address:
                self.contract = self.w3.eth.contract(
                    address=Web3.to_checksum_address(self.contract_address),
                    abi=CONTRACT_ABI
                )
                logger.info(f"📋 Learn & Earn Contract loaded: {self.contract_address}")
            else:
                logger.warning("⚠️ LEARN_EARN_CONTRACT_ADDRESS not set - deploy contract first")

            self.gooddollar_contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_address),
                abi=ERC20_APPROVE_ABI
            )

            if self.learn_wallet_key:
                if self.learn_wallet_key.startswith('0x'):
                    self.owner_account = Account.from_key(self.learn_wallet_key)
                else:
                    self.owner_account = Account.from_key('0x' + self.learn_wallet_key)
                logger.info(f"👛 Owner wallet: {self.owner_account.address}")
            else:
                logger.error("❌ LEARN_WALLET_PRIVATE_KEY not configured")

        except Exception as e:
            logger.error(f"❌ Initialization error: {e}")

    def _send_transaction(self, txn_builder, gas_limit=300000):
        """Build, sign, and send a transaction"""
        try:
            nonce = self.w3.eth.get_transaction_count(self.owner_account.address)
            gas_price = int(self.w3.eth.gas_price * 1.2)

            txn = txn_builder.build_transaction({
                'chainId': self.chain_id,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
            })

            signed_txn = self.w3.eth.account.sign_transaction(txn, self.learn_wallet_key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            logger.info(f"📡 Transaction sent: {tx_hash_hex}")

            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            return {
                "success": receipt.status == 1,
                "tx_hash": tx_hash_hex,
                "gas_used": receipt.gasUsed,
                "block_number": receipt.blockNumber,
                "explorer_url": f"https://celoscan.io/tx/{tx_hash_hex}"
            }

        except Exception as e:
            logger.error(f"❌ Transaction error: {e}")
            return {"success": False, "error": str(e)}

    async def deposit_g_dollars(self, amount: float) -> dict:
        """
        Deposit G$ tokens into the contract
        
        Args:
            amount: Amount in G$ to deposit
            
        Returns:
            Dict with transaction result
        """
        try:
            if not self.contract:
                return {"success": False, "error": "Contract not initialized"}

            amount_wei = int(amount * 10**18)
            
            logger.info(f"💰 Depositing {amount} G$ to contract...")

            allowance = self.gooddollar_contract.functions.allowance(
                self.owner_account.address,
                self.contract_address
            ).call()

            if allowance < amount_wei:
                logger.info("🔓 Approving G$ spending...")
                approve_result = self._send_transaction(
                    self.gooddollar_contract.functions.approve(
                        Web3.to_checksum_address(self.contract_address),
                        amount_wei
                    ),
                    gas_limit=100000
                )
                if not approve_result["success"]:
                    return {"success": False, "error": "Approval failed", "details": approve_result}

            deposit_result = self._send_transaction(
                self.contract.functions.deposit(amount_wei),
                gas_limit=200000
            )

            if deposit_result["success"]:
                logger.info(f"✅ Deposited {amount} G$ successfully")

            return deposit_result

        except Exception as e:
            logger.error(f"❌ Deposit error: {e}")
            return {"success": False, "error": str(e)}

    async def disburse_reward(self, recipient: str, amount: float, quiz_id: str) -> dict:
        """
        Disburse G$ reward to a recipient using the smart contract
        
        Args:
            recipient: Wallet address of the recipient
            amount: Amount in G$ to send
            quiz_id: Quiz identifier for tracking
            
        Returns:
            Dict with transaction result
        """
        try:
            if not self.contract:
                return {"success": False, "error": "Contract not initialized"}

            amount_wei = int(amount * 10**18)
            
            logger.info(f"💰 Disbursing {amount} G$ to {recipient[:8]}... via contract")

            contract_balance = self.contract.functions.getContractBalance().call()
            if contract_balance < amount_wei:
                logger.error(f"❌ Insufficient contract balance: {contract_balance / 10**18} G$")
                return {
                    "success": False,
                    "error": "Insufficient contract balance",
                    "insufficient_balance": True
                }

            result = self._send_transaction(
                self.contract.functions.disburseReward(
                    Web3.to_checksum_address(recipient),
                    amount_wei,
                    quiz_id
                ),
                gas_limit=300000
            )

            if result["success"]:
                logger.info(f"✅ SMART CONTRACT SUCCESS: {amount} G$ disbursed - TX: {result['tx_hash']}")

            return result

        except Exception as e:
            logger.error(f"❌ Disbursement error: {e}")
            return {"success": False, "error": str(e)}

    async def batch_disburse_rewards(self, recipients: list, amounts: list, quiz_ids: list) -> dict:
        """
        Batch disburse G$ rewards to multiple recipients
        
        Args:
            recipients: List of wallet addresses
            amounts: List of amounts in G$
            quiz_ids: List of quiz identifiers
            
        Returns:
            Dict with transaction result
        """
        try:
            if not self.contract:
                return {"success": False, "error": "Contract not initialized"}

            if len(recipients) != len(amounts) or len(recipients) != len(quiz_ids):
                return {"success": False, "error": "Arrays length mismatch"}

            if len(recipients) > 50:
                return {"success": False, "error": "Batch too large (max 50)"}

            amounts_wei = [int(a * 10**18) for a in amounts]
            total_amount = sum(amounts_wei)
            
            logger.info(f"💰 Batch disbursing to {len(recipients)} recipients...")

            contract_balance = self.contract.functions.getContractBalance().call()
            if contract_balance < total_amount:
                return {
                    "success": False,
                    "error": "Insufficient contract balance",
                    "insufficient_balance": True
                }

            recipients_checksum = [Web3.to_checksum_address(r) for r in recipients]

            result = self._send_transaction(
                self.contract.functions.batchDisburseRewards(
                    recipients_checksum,
                    amounts_wei,
                    quiz_ids
                ),
                gas_limit=500000 + (len(recipients) * 50000)
            )

            if result["success"]:
                logger.info(f"✅ Batch disbursement successful - TX: {result['tx_hash']}")

            return result

        except Exception as e:
            logger.error(f"❌ Batch disbursement error: {e}")
            return {"success": False, "error": str(e)}

    async def withdraw_g_dollars(self, amount: float) -> dict:
        """
        Withdraw G$ tokens from the contract (owner only)
        
        Args:
            amount: Amount in G$ to withdraw
            
        Returns:
            Dict with transaction result
        """
        try:
            if not self.contract:
                return {"success": False, "error": "Contract not initialized"}

            amount_wei = int(amount * 10**18)
            
            logger.info(f"💸 Withdrawing {amount} G$ from contract...")

            result = self._send_transaction(
                self.contract.functions.withdraw(amount_wei),
                gas_limit=200000
            )

            if result["success"]:
                logger.info(f"✅ Withdrew {amount} G$ successfully")

            return result

        except Exception as e:
            logger.error(f"❌ Withdraw error: {e}")
            return {"success": False, "error": str(e)}

    async def withdraw_all(self) -> dict:
        """Withdraw all G$ tokens from the contract (owner only)"""
        try:
            if not self.contract:
                return {"success": False, "error": "Contract not initialized"}

            logger.info("💸 Withdrawing all G$ from contract...")

            result = self._send_transaction(
                self.contract.functions.withdrawAll(),
                gas_limit=200000
            )

            if result["success"]:
                logger.info("✅ Withdrew all G$ successfully")

            return result

        except Exception as e:
            logger.error(f"❌ Withdraw all error: {e}")
            return {"success": False, "error": str(e)}

    def get_contract_balance(self) -> float:
        """Get the G$ balance of the contract"""
        try:
            if not self.contract:
                return 0.0

            balance_wei = self.contract.functions.getContractBalance().call()
            return balance_wei / 10**18

        except Exception as e:
            logger.error(f"❌ Error getting contract balance: {e}")
            return 0.0

    def get_contract_stats(self) -> dict:
        """Get contract statistics"""
        try:
            if not self.contract:
                return {}

            stats = self.contract.functions.getContractStats().call()
            
            return {
                "balance": stats[0] / 10**18,
                "total_deposited": stats[1] / 10**18,
                "total_disbursed": stats[2] / 10**18,
                "total_withdrawn": stats[3] / 10**18
            }

        except Exception as e:
            logger.error(f"❌ Error getting contract stats: {e}")
            return {}

    def get_user_stats(self, user_address: str) -> dict:
        """Get user reward statistics"""
        try:
            if not self.contract:
                return {}

            stats = self.contract.functions.getUserStats(
                Web3.to_checksum_address(user_address)
            ).call()
            
            return {
                "total_rewards": stats[0] / 10**18,
                "reward_count": stats[1]
            }

        except Exception as e:
            logger.error(f"❌ Error getting user stats: {e}")
            return {}

    def is_paused(self) -> bool:
        """Check if contract is paused"""
        try:
            if not self.contract:
                return True
            return self.contract.functions.paused().call()
        except Exception as e:
            logger.error(f"❌ Error checking paused status: {e}")
            return True

    def is_quiz_reward_claimed(self, recipient: str, quiz_id: str) -> bool:
        """
        Check if a reward was already claimed for a specific quiz
        
        Args:
            recipient: Wallet address of the user
            quiz_id: Quiz identifier
            
        Returns:
            True if reward was already claimed
        """
        try:
            if not self.contract:
                return False

            is_claimed = self.contract.functions.isQuizRewardClaimed(
                Web3.to_checksum_address(recipient),
                quiz_id
            ).call()
            
            return is_claimed

        except Exception as e:
            logger.error(f"❌ Error checking quiz reward status: {e}")
            return False

    def get_reward_id(self, recipient: str, quiz_id: str) -> str:
        """
        Get the deterministic reward ID for a recipient + quiz combination
        
        Args:
            recipient: Wallet address of the user
            quiz_id: Quiz identifier
            
        Returns:
            Reward ID as hex string
        """
        try:
            if not self.contract:
                return ""

            reward_id = self.contract.functions.getRewardId(
                Web3.to_checksum_address(recipient),
                quiz_id
            ).call()
            
            return reward_id.hex()

        except Exception as e:
            logger.error(f"❌ Error getting reward ID: {e}")
            return ""


learn_earn_contract_service = LearnEarnContractService()
