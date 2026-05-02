import os
import logging

from web3 import Web3
from eth_account import Account


logger = logging.getLogger(__name__)


# USDT (Tether) on Celo — same address used elsewhere in blockchain.py.
# 6 decimals.
USDT_CONTRACT = os.getenv(
    "USDT_CONTRACT",
    "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e",
)
USDT_DECIMALS = 6


# Minimal ERC-20 ABI for `transfer`.
_ERC20_TRANSFER_ABI = [
    {
        "constant": False,
        "inputs": [
            {"name": "_to", "type": "address"},
            {"name": "_value", "type": "uint256"},
        ],
        "name": "transfer",
        "outputs": [{"name": "", "type": "bool"}],
        "type": "function",
    }
]


class DailyCheckinBlockchainService:
    """Daily Check-in disbursement service.

    Uses ``CHECKIN_KEY`` (the dedicated check-in hot wallet private key)
    for both the source-of-funds AND gas. Falls back to the legacy
    ``TASK_KEY`` for backwards compatibility with older deployments.
    """

    def __init__(self):
        self.celo_rpc_url = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
        self.chain_id = int(os.getenv("CHAIN_ID", 42220))

        # CHECKIN_KEY is the canonical name; TASK_KEY kept as fallback so
        # existing deployments keep working without a redeploy.
        raw_key = os.getenv("CHECKIN_KEY") or os.getenv("TASK_KEY")
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        self.checkin_account = None
        self._key_hex = None
        if raw_key:
            key = raw_key if raw_key.startswith("0x") else "0x" + raw_key
            try:
                self.checkin_account = Account.from_key(key)
                self._key_hex = key
                logger.info(
                    "Daily Check-in service ready (signer=%s)",
                    self.checkin_account.address[:6] + "..." + self.checkin_account.address[-4:],
                )
            except Exception as exc:  # pragma: no cover - misconfigured key
                logger.error("Failed to load CHECKIN_KEY/TASK_KEY: %s", exc)
                self.checkin_account = None
                self._key_hex = None
        else:
            logger.warning("CHECKIN_KEY not set — daily check-in disbursements disabled")

    # ------------------------------------------------------------------
    # Native CELO disbursement (legacy / non-MiniPay users)
    # ------------------------------------------------------------------
    def send_celo(self, recipient: str, amount_celo: float) -> dict:
        if not self.checkin_account:
            return {"success": False, "error": "CHECKIN_KEY not configured"}
        if not self.w3.is_connected():
            return {"success": False, "error": "Blockchain connection failed"}

        to_addr = Web3.to_checksum_address(recipient)
        from_addr = self.checkin_account.address
        value_wei = self.w3.to_wei(amount_celo, "ether")

        gas_price = self.w3.eth.gas_price
        nonce = self.w3.eth.get_transaction_count(from_addr, "pending")
        tx = {
            "chainId": self.chain_id,
            "nonce": nonce,
            "to": to_addr,
            "value": value_wei,
            "gas": 21000,
            "gasPrice": gas_price,
        }

        signed = self.w3.eth.account.sign_transaction(tx, self.checkin_account.key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        return {
            "success": receipt.status == 1,
            "tx_hash": tx_hash.hex(),
            "status": receipt.status,
            "amount": amount_celo,
            "token": "CELO",
        }

    # ------------------------------------------------------------------
    # USDT disbursement (MiniPay users — they live in a USD-denominated UX)
    # ------------------------------------------------------------------
    def send_usdt(self, recipient: str, amount_usdt: float) -> dict:
        """Send USDT from the CHECKIN_KEY wallet. Gas is paid in native CELO
        from the same wallet, so it must hold a small CELO balance for fees.
        """
        if not self.checkin_account or not self._key_hex:
            return {"success": False, "error": "CHECKIN_KEY not configured"}
        if not self.w3.is_connected():
            return {"success": False, "error": "Blockchain connection failed"}
        if amount_usdt <= 0:
            return {"success": False, "error": "Amount must be positive"}

        try:
            to_addr = Web3.to_checksum_address(recipient)
            from_addr = self.checkin_account.address
            token_addr = Web3.to_checksum_address(USDT_CONTRACT)

            contract = self.w3.eth.contract(address=token_addr, abi=_ERC20_TRANSFER_ABI)
            amount_units = int(round(amount_usdt * (10 ** USDT_DECIMALS)))
            if amount_units <= 0:
                return {"success": False, "error": "Amount rounds to zero USDT"}

            nonce = self.w3.eth.get_transaction_count(from_addr, "pending")
            gas_price = int(self.w3.eth.gas_price * 1.2)

            tx = contract.functions.transfer(to_addr, amount_units).build_transaction({
                "chainId": self.chain_id,
                "from": from_addr,
                "nonce": nonce,
                "gas": 120000,
                "gasPrice": gas_price,
            })

            signed = self.w3.eth.account.sign_transaction(tx, self._key_hex)
            tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)

            tx_hash_hex = tx_hash.hex()
            if not tx_hash_hex.startswith("0x"):
                tx_hash_hex = "0x" + tx_hash_hex

            return {
                "success": receipt.status == 1,
                "tx_hash": tx_hash_hex,
                "status": receipt.status,
                "amount": amount_usdt,
                "token": "USDT",
                "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}",
            }
        except Exception as exc:
            logger.error("USDT disbursement failed: %s", exc, exc_info=True)
            return {"success": False, "error": f"USDT transfer error: {exc}"}


daily_checkin_blockchain = DailyCheckinBlockchainService()
