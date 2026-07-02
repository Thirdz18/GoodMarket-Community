from env_utils import get_env_float, get_env_int
import os
import logging
from web3 import Web3
from eth_account import Account

logger = logging.getLogger(__name__)

ERC20_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"}
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function"
    },
    {
        "constant": True,
        "inputs": [{"name": "_owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "balance", "type": "uint256"}],
        "type": "function"
    }
]


class ReferralBlockchain:
    """Handles G$ disbursement for the referral program using REFERRAL_KEY."""

    def __init__(self):
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = get_env_int('CHAIN_ID', 42220)
        self.gooddollar_token = os.getenv(
            'GOODDOLLAR_TOKEN_CONTRACT',
            '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A'
        )
        self.referral_key = os.getenv('REFERRAL_KEY')
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("Referral blockchain service connected to Celo network")
        else:
            logger.error("Referral blockchain service failed to connect to Celo network")

        if not self.referral_key:
            logger.error("REFERRAL_KEY environment variable not set")

    def _mask(self, addr):
        if not addr or len(addr) < 10:
            return addr
        return addr[:6] + "..." + addr[-4:]

    def get_referral_wallet_balance(self):
        """Return the G$ balance of the REFERRAL_KEY wallet."""
        if not self.referral_key:
            return {"success": False, "error": "REFERRAL_KEY not configured", "balance": 0}
        try:
            key = self.referral_key if self.referral_key.startswith('0x') else '0x' + self.referral_key
            account = Account.from_key(key)
            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_token),
                abi=ERC20_ABI
            )
            balance_wei = contract.functions.balanceOf(account.address).call()
            balance_g = balance_wei / (10 ** 18)
            return {
                "success": True,
                "balance": balance_g,
                "balance_wei": balance_wei,
                "wallet": account.address
            }
        except Exception as e:
            logger.error(f"Failed to get referral wallet balance: {e}")
            return {"success": False, "error": str(e), "balance": 0}

    def disburse_referral_reward(self, wallet_address: str, amount: float, reward_type: str) -> dict:
        """
        Transfer G$ from REFERRAL_KEY wallet to recipient.
        Returns {"success": True, "tx_hash": "..."} on success.
        Returns {"success": False, "pending": True, "error": "insufficient_balance"} when out of funds.
        """
        try:
            if not self.referral_key:
                logger.error("REFERRAL_KEY not configured")
                return {"success": False, "pending": True, "error": "REFERRAL_KEY not configured"}

            key = self.referral_key if self.referral_key.startswith('0x') else '0x' + self.referral_key
            try:
                referral_account = Account.from_key(key)
            except Exception as key_err:
                logger.error(f"Invalid REFERRAL_KEY: {key_err}")
                return {"success": False, "pending": False, "error": "Invalid REFERRAL_KEY"}

            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_token),
                abi=ERC20_ABI
            )

            amount_wei = int(amount * (10 ** 18))

            balance_wei = contract.functions.balanceOf(referral_account.address).call()
            balance_g = balance_wei / (10 ** 18)
            logger.info(
                f"Referral wallet balance: {balance_g:.2f} G$ | "
                f"Required: {amount:.2f} G$ ({reward_type} for {self._mask(wallet_address)})"
            )

            if balance_wei < amount_wei:
                logger.warning(
                    f"Insufficient REFERRAL_KEY balance: {balance_g:.2f} G$ < {amount:.2f} G$. "
                    f"Marking as pending_disbursed."
                )
                return {
                    "success": False,
                    "pending": True,
                    "error": "insufficient_balance",
                    "balance_available": balance_g,
                    "balance_required": amount
                }

            logger.info(f"[BLOCKCHAIN] Balance OK: {balance_g:.2f} G$ available, need {amount:.2f} G$")

            nonce = self.w3.eth.get_transaction_count(referral_account.address)
            logger.info(f"[BLOCKCHAIN] nonce={nonce}")
            gas_price = int(self.w3.eth.gas_price * 1.2)

            # Estimate gas dynamically instead of hardcoding a fixed limit.
            # G$ ERC-777 hooks add overhead vs plain ERC-20, so apply a 1.3x
            # safety buffer on top of the estimate, and fall back to a
            # conservative ceiling only if estimation fails.
            try:
                estimated_gas = contract.functions.transfer(
                    Web3.to_checksum_address(wallet_address),
                    amount_wei
                ).estimate_gas({'from': referral_account.address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"Referral reward gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"Gas estimation failed, falling back to 250000: {estimate_error}"
                )
                gas_limit = 250000

            txn = contract.functions.transfer(
                Web3.to_checksum_address(wallet_address),
                amount_wei
            ).build_transaction({
                'chainId': self.chain_id,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
                'from': referral_account.address
            })

            signed_txn = self.w3.eth.account.sign_transaction(txn, key)
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            logger.info(f"[BLOCKCHAIN] TX SENT: {tx_hash_hex}")
            logger.info(f"[BLOCKCHAIN] Waiting for receipt...")

            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
            logger.info(f"[BLOCKCHAIN] Receipt: status={receipt.status}")

            if receipt.status == 1:
                logger.info(
                    f"Referral {reward_type} reward of {amount} G$ sent to "
                    f"{self._mask(wallet_address)} | TX: {tx_hash_hex}"
                )
                return {
                    "success": True,
                    "pending": False,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "recipient": wallet_address,
                    "reward_type": reward_type
                }
            else:
                logger.error(f"Referral reward TX failed on-chain: {tx_hash_hex}")
                return {"success": False, "pending": False, "error": "Transaction failed on-chain", "tx_hash": tx_hash_hex}

        except Exception as e:
            logger.error(f"Referral reward disbursement error for {reward_type}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {"success": False, "pending": False, "error": str(e)}

    def disburse_referral_reward_sync(self, wallet_address: str, amount: float, reward_type: str) -> dict:
        """Synchronous wrapper (runs in current thread)."""
        return self.disburse_referral_reward(wallet_address, amount, reward_type)


referral_blockchain_service = ReferralBlockchain()
