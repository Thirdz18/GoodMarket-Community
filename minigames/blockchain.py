from env_utils import get_env_float, get_env_int
import os
import logging
from web3 import Web3
from eth_account import Account
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

class MinigamesBlockchainService:
    """Minigames Blockchain Service for G$ Rewards using GamesRewards Smart Contract"""

    def __init__(self):
        # Network configuration
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = get_env_int('CHAIN_ID', 42220)
        self.gooddollar_contract = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
        
        # GamesRewards Smart Contract (deployed contract for secure disbursement)
        self.games_rewards_contract = os.getenv('GAMES_REWARDS_CONTRACT', '0x230770E2a19b33923a046de107e41cA00F55685F')

        # MERCHANT_ADDRESS for deposits (users send G$ here)
        merchant_address = os.getenv('MERCHANT_ADDRESS')
        if merchant_address:
            try:
                self.merchant_address = Web3.to_checksum_address(merchant_address)
                logger.info(f"✅ MERCHANT_ADDRESS configured: {self.merchant_address}")
            except Exception as e:
                logger.error(f"❌ Error loading MERCHANT_ADDRESS: {e}")
                self.merchant_address = None
        else:
            self.merchant_address = None
            logger.warning("⚠️ MERCHANT_ADDRESS not configured")

        # Server wallet for signing transactions (authorized disburser)
        server_private_key = os.getenv('SERVER_PRIVATE_KEY')
        if server_private_key:
            try:
                if not server_private_key.startswith('0x'):
                    server_private_key = '0x' + server_private_key
                self.server_account = Account.from_key(server_private_key)
                self.server_address = self.server_account.address
                logger.info(f"✅ SERVER wallet configured: {self.server_address}")
            except Exception as e:
                logger.error(f"❌ Error loading SERVER_PRIVATE_KEY: {e}")
                self.server_account = None
                self.server_address = None
        else:
            self.server_account = None
            self.server_address = None
            logger.warning("⚠️ SERVER_PRIVATE_KEY not configured")

        # Initialize Web3
        self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))

        if self.w3.is_connected():
            logger.info("✅ Connected to Celo network for Minigames")
        else:
            logger.error("❌ Failed to connect to Celo network")

        # GoodDollar token contract
        self.gooddollar_token = Web3.to_checksum_address(self.gooddollar_contract)
        
        # GamesRewards contract
        self.games_rewards_address = Web3.to_checksum_address(self.games_rewards_contract)

        # ERC20 ABI for transfers
        self.erc20_abi = [
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
        
        # GamesRewards contract ABI
        self.games_rewards_abi = [
            {
                "inputs": [
                    {"name": "recipient", "type": "address"},
                    {"name": "amount", "type": "uint256"},
                    {"name": "sessionId", "type": "string"}
                ],
                "name": "disburseReward",
                "outputs": [{"name": "", "type": "bool"}],
                "stateMutability": "nonpayable",
                "type": "function"
            },
            {
                "inputs": [{"name": "user", "type": "address"}],
                "name": "getRemainingDailyLimit",
                "outputs": [{"name": "", "type": "uint256"}],
                "stateMutability": "view",
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
                "inputs": [],
                "name": "paused",
                "outputs": [{"name": "", "type": "bool"}],
                "stateMutability": "view",
                "type": "function"
            },
            {
                "inputs": [],
                "name": "owner",
                "outputs": [{"name": "", "type": "address"}],
                "stateMutability": "view",
                "type": "function"
            }
        ]

        self.token_contract = self.w3.eth.contract(
            address=self.gooddollar_token,
            abi=self.erc20_abi
        )
        
        self.rewards_contract = self.w3.eth.contract(
            address=self.games_rewards_address,
            abi=self.games_rewards_abi
        )

        # Transfer event signature
        self.TRANSFER_EVENT_SIGNATURE = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

        logger.info(f"🎮 Minigames Blockchain Service initialized (Smart Contract Mode)")
        logger.info(f"   MERCHANT address (deposits): {self.merchant_address}")
        logger.info(f"   SERVER wallet address: {self.server_address}")
        logger.info(f"   GamesRewards contract: {self.games_rewards_address}")
        logger.info(f"   GoodDollar token: {self.gooddollar_token}")


    def mask_wallet_address(self, wallet_address: str) -> str:
        """Mask wallet address for logging"""
        if not wallet_address or len(wallet_address) < 10:
            return wallet_address
        return wallet_address[:6] + "..." + wallet_address[-4:]

    async def verify_deposit_to_merchant(self, wallet_address: str, amount: float, tx_hash: str) -> dict:
        """Verify that user deposited G$ to MERCHANT_ADDRESS"""
        try:
            logger.info(f"🔍 Verifying deposit: {amount} G$ from {self.mask_wallet_address(wallet_address)}")

            if not self.w3.is_connected():
                return {"success": False, "error": "Blockchain connection failed"}

            if not self.merchant_address:
                logger.error("❌ MERCHANT_ADDRESS not configured.")
                return {"success": False, "error": "MERCHANT_ADDRESS not configured"}

            # Get transaction receipt
            receipt = self.w3.eth.get_transaction_receipt(tx_hash)

            if not receipt or receipt.status != 1:
                return {"success": False, "error": "Transaction not found or failed"}

            # Check transfer logs for the specific token contract
            for log in receipt.logs:
                if log['address'].lower() == self.gooddollar_token.lower():
                    # Check if it's a Transfer event
                    if len(log['topics']) >= 3:
                        # Topics: [event_signature, from_address, to_address]
                        to_address = '0x' + log['topics'][2].hex()[-40:]

                        if to_address.lower() == self.merchant_address.lower():
                            # Verify amount
                            amount_wei = int(log['data'].hex(), 16)
                            amount_g = amount_wei / (10 ** 18)

                            if abs(amount_g - amount) < 0.01:  # Allow small variance
                                logger.info(f"✅ Deposit verified: {amount} G$ to MERCHANT_ADDRESS")
                                return {"success": True, "verified": True, "amount": amount_g, "tx_hash": tx_hash}

            return {"success": False, "error": "Transfer to MERCHANT_ADDRESS not found in transaction"}

        except Exception as e:
            logger.error(f"❌ Error verifying deposit: {e}")
            return {"success": False, "error": str(e)}

    async def check_pending_deposits(self, wallet_address: str, expected_amount: float = None) -> dict:
        """
        Automatically check for pending deposits to MERCHANT_ADDRESS from a wallet
        Similar to P2P trading's automatic deposit verification
        """
        try:
            logger.info(f"🔍 AUTO-VERIFY: Checking deposits from {self.mask_wallet_address(wallet_address)} to MERCHANT_ADDRESS")

            if not self.w3.is_connected():
                return {'success': False, 'error': 'Blockchain connection failed', 'deposits_found': []}

            if not self.merchant_address:
                logger.error("❌ MERCHANT_ADDRESS not configured.")
                return {'success': False, 'error': 'MERCHANT_ADDRESS not configured', 'deposits_found': []}

            # Calculate block range (last 24 hours)
            latest_block = self.w3.eth.block_number
            # Assuming Celo block time is around 5 seconds, 720 blocks per hour
            blocks_per_hour = 720
            # Look back for 24 hours
            hours_to_check = 24
            from_block = max(0, latest_block - (hours_to_check * blocks_per_hour))


            logger.info(f"📊 Scanning blocks {from_block} to {latest_block} (last {hours_to_check} hours)")

            # Convert addresses to topic format for logs
            # Topic[0] is the event signature
            # Topic[1] is the indexed parameter 'from' (sender)
            # Topic[2] is the indexed parameter 'to' (recipient)
            from_topic = '0x' + '0' * 24 + wallet_address.lower().replace('0x', '')
            to_topic = '0x' + '0' * 24 + self.merchant_address.lower().replace('0x', '')

            # Query Transfer events: FROM user TO MERCHANT_ADDRESS
            filter_params = {
                'fromBlock': hex(from_block),
                'toBlock': 'latest',
                'address': self.gooddollar_token,
                'topics': [
                    self.TRANSFER_EVENT_SIGNATURE,
                    from_topic,  # FROM: user wallet
                    to_topic     # TO: MERCHANT_ADDRESS
                ]
            }

            logs = self.w3.eth.get_logs(filter_params)
            logger.info(f"📋 Found {len(logs)} G$ transfers from {self.mask_wallet_address(wallet_address)} to MERCHANT_ADDRESS")

            deposits = []
            for log in logs:
                try:
                    # Parse amount from the event data
                    amount_wei = int(log['data'].hex(), 16)
                    amount_g = amount_wei / (10 ** 18)

                    # Get block timestamp for context
                    block = self.w3.eth.get_block(log['blockNumber'])
                    timestamp = datetime.fromtimestamp(block['timestamp'])

                    tx_hash = log['transactionHash'].hex()

                    deposit_info = {
                        'tx_hash': tx_hash,
                        'amount': amount_g,
                        'block_number': log['blockNumber'],
                        'timestamp': timestamp.isoformat(),
                        'from': wallet_address,
                        'to': self.merchant_address
                    }

                    # If an expected amount is specified, check if the deposit matches
                    if expected_amount is not None:
                        if abs(amount_g - expected_amount) < 0.01:  # Allow small rounding difference
                            deposits.append(deposit_info)
                            logger.info(f"✅ Matching deposit: {amount_g} G$ (TX: {tx_hash[:16]}...)")
                    else:
                        # If no specific amount is expected, add all found deposits
                        deposits.append(deposit_info)
                        logger.info(f"📦 Deposit found: {amount_g} G$ (TX: {tx_hash[:16]}...)")

                except Exception as parse_error:
                    logger.error(f"❌ Error parsing log entry: {parse_error}")
                    # Continue to the next log entry even if one fails
                    continue

            if len(deposits) > 0:
                logger.info(f"✅ Successfully found {len(deposits)} deposit(s) from {self.mask_wallet_address(wallet_address)}.")
                # Return the list of deposits, count, and the most recent one
                return {
                    'success': True,
                    'deposits_found': deposits,
                    'total_deposits': len(deposits),
                    'latest_deposit': deposits[0] if deposits else None
                }
            else:
                logger.info(f"⏳ No matching deposits found from {self.mask_wallet_address(wallet_address)} to MERCHANT_ADDRESS in the last {hours_to_check} hours.")
                return {
                    'success': True,
                    'deposits_found': [],
                    'total_deposits': 0,
                    'latest_deposit': None
                }

        except Exception as e:
            logger.error(f"❌ An unexpected error occurred while checking pending deposits: {e}")
            # Return error and an empty list of deposits
            return {'success': False, 'error': str(e), 'deposits_found': []}


    async def disburse_from_games_key(self, wallet_address: str, amount: float, session_id: str) -> dict:
        """Disburse winnings via GamesRewards Smart Contract"""
        try:
            logger.info(f"💸 Disbursing winnings via contract: {amount} G$ to {self.mask_wallet_address(wallet_address)}")

            if not self.server_account:
                logger.error("❌ SERVER_PRIVATE_KEY not configured")
                return {"success": False, "error": "Server wallet not configured"}

            if not self.w3.is_connected():
                return {"success": False, "error": "Blockchain connection failed"}

            # Check if contract is paused
            try:
                is_paused = self.rewards_contract.functions.paused().call()
                if is_paused:
                    return {"success": False, "error": "Rewards contract is temporarily paused"}
            except:
                pass

            recipient_checksum = Web3.to_checksum_address(wallet_address)
            amount_wei = int(amount * (10 ** 18))

            # Build transaction via contract
            nonce = self.w3.eth.get_transaction_count(self.server_address)
            gas_price = int(self.w3.eth.gas_price * 1.2)  # Add 20% buffer

            # Estimate gas for contract call
            try:
                estimated_gas = self.rewards_contract.functions.disburseReward(
                    recipient_checksum,
                    amount_wei,
                    session_id
                ).estimate_gas({'from': self.server_address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"⛽ Withdrawal gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"⚠️ Gas estimation failed, falling back to 200000: {estimate_error}"
                )
                gas_limit = 200000

            transaction = self.rewards_contract.functions.disburseReward(
                recipient_checksum,
                amount_wei,
                session_id
            ).build_transaction({
                'from': self.server_address,
                'nonce': nonce,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'chainId': self.chain_id
            })

            # Sign and send
            signed_txn = self.w3.eth.account.sign_transaction(
                transaction,
                private_key=self.server_account.key
            )

            logger.info("📡 Sending withdrawal transaction via contract...")
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                logger.info(f"✅ Withdrawal successful via contract: {amount} G$ - TX: {tx_hash_hex}")

                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "recipient": wallet_address,
                    "message": f"Successfully withdrew {amount} G$!",
                    "explorer_url": f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}",
                    "contract_address": self.games_rewards_address
                }
            else:
                logger.error(f"❌ Withdrawal failed on-chain: {tx_hash_hex}")
                return {"success": False, "error": "Transaction failed on blockchain", "tx_hash": tx_hash_hex}

        except Exception as e:
            import traceback
            logger.error(f"❌ Withdrawal error: {e}")
            logger.error(f"🔍 Traceback: {traceback.format_exc()}")
            
            # Check for insufficient funds error
            error_msg = str(e).lower()
            if "insufficient funds" in error_msg:
                logger.error(f"❌ Server wallet needs CELO for gas fees!")
                return {
                    "success": False, 
                    "error": "Withdrawal system temporarily unavailable. Please try again later or contact support.",
                    "error_type": "insufficient_gas",
                    "balance_safe": True
                }
            
            return {"success": False, "error": "Withdrawal failed. Please try again later."}

    async def disburse_game_reward(self, wallet_address: str, amount: float, game_type: str, session_id: str) -> dict:
        """
        Disburse game reward to player via GamesRewards Smart Contract

        Args:
            wallet_address: Recipient wallet address
            amount: Amount in G$ to disburse
            game_type: Type of game (for logging)
            session_id: Game session ID

        Returns:
            Dict with success status, transaction hash, and details
        """
        try:
            logger.info(f"🎮 Minigame reward disbursement via contract: {amount} G$ to {self.mask_wallet_address(wallet_address)}")

            if not self.server_account:
                logger.error("❌ SERVER_PRIVATE_KEY not configured for minigames rewards")
                return {"success": False, "error": "Server wallet not configured"}

            if not self.w3.is_connected():
                logger.error("❌ Not connected to Celo network")
                return {"success": False, "error": "Blockchain connection failed"}

            # Check if contract is paused
            try:
                is_paused = self.rewards_contract.functions.paused().call()
                if is_paused:
                    logger.error("❌ GamesRewards contract is paused")
                    return {"success": False, "error": "Rewards contract is temporarily paused"}
            except Exception as e:
                logger.warning(f"⚠️ Could not check contract pause status: {e}")

            # Convert amount to Wei (18 decimals for G$)
            amount_wei = int(amount * (10 ** 18))

            # Check remaining daily limit for user
            try:
                remaining = self.rewards_contract.functions.getRemainingDailyLimit(
                    Web3.to_checksum_address(wallet_address)
                ).call()
                if amount_wei > remaining:
                    logger.warning(f"⚠️ Amount {amount} exceeds daily limit. Remaining: {remaining / (10**18)} G$")
                    return {
                        "success": False, 
                        "error": f"Daily limit exceeded. Remaining: {remaining / (10**18):.2f} G$"
                    }
            except Exception as e:
                logger.warning(f"⚠️ Could not check daily limit: {e}")

            # Get nonce and gas price for the transaction
            nonce = self.w3.eth.get_transaction_count(self.server_address)
            gas_price = int(self.w3.eth.gas_price * 1.2)  # Add 20% buffer for gas price

            # Estimate gas for contract call
            try:
                estimated_gas = self.rewards_contract.functions.disburseReward(
                    Web3.to_checksum_address(wallet_address),
                    amount_wei,
                    session_id
                ).estimate_gas({'from': self.server_address})
                gas_limit = int(estimated_gas * 1.3)
                logger.info(
                    f"⛽ Contract call gas estimate: {estimated_gas} "
                    f"(using limit: {gas_limit})"
                )
            except Exception as estimate_error:
                logger.warning(
                    f"⚠️ Gas estimation failed, falling back to 200000: {estimate_error}"
                )
                gas_limit = 200000

            # Build the transaction using the GamesRewards contract
            transaction = self.rewards_contract.functions.disburseReward(
                Web3.to_checksum_address(wallet_address),
                amount_wei,
                session_id
            ).build_transaction({
                'from': self.server_address,
                'gas': gas_limit,
                'gasPrice': gas_price,
                'nonce': nonce,
                'chainId': self.chain_id
            })

            # Sign the transaction with the server private key
            signed_txn = self.w3.eth.account.sign_transaction(
                transaction,
                private_key=self.server_account.key
            )

            # Send the signed transaction to the network
            logger.info("📡 Sending reward transaction via contract...")
            tx_hash = self.w3.eth.send_raw_transaction(signed_txn.raw_transaction)
            tx_hash_hex = tx_hash.hex()

            # Ensure tx_hash starts with '0x'
            if not tx_hash_hex.startswith('0x'):
                tx_hash_hex = '0x' + tx_hash_hex

            logger.info(f"🔗 Transaction sent: {tx_hash_hex}")

            # Wait for the transaction to be confirmed on the blockchain
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)

            if receipt.status == 1:
                # Transaction successful
                logger.info(f"✅ Minigame reward successfully disbursed via contract: {amount} G$ - TX: {tx_hash_hex}")
                explorer_url = f"https://explorer.celo.org/mainnet/tx/{tx_hash_hex}"
                logger.info(f"🔗 Explorer: {explorer_url}")
                logger.info(f"⛽ Gas used: {receipt.gasUsed}")
                logger.info(f"🧾 Block: {receipt.blockNumber}")
                logger.info(f"📜 Contract: {self.games_rewards_address}")

                return {
                    "success": True,
                    "tx_hash": tx_hash_hex,
                    "amount": amount,
                    "game_type": game_type,
                    "session_id": session_id,
                    "recipient": wallet_address,
                    "message": f"Successfully disbursed {amount} G$ minigame reward!",
                    "timestamp": datetime.now().isoformat(),
                    "explorer_url": explorer_url,
                    "contract_address": self.games_rewards_address,
                    "blockchain_confirmed": True
                }
            else:
                # Transaction failed on the blockchain
                logger.error(f"❌ Minigame transaction failed on-chain: {tx_hash_hex}")
                return {
                    "success": False,
                    "error": "Transaction failed on blockchain",
                    "tx_hash": tx_hash_hex
                }

        except Exception as e:
            # Log any exceptions during the disbursement process
            import traceback
            logger.error(f"❌ Minigame reward disbursement error: {e}")
            logger.error(f"🔍 Traceback: {traceback.format_exc()}")
            return {"success": False, "error": str(e)}

# Global instance for the service
minigames_blockchain = MinigamesBlockchainService()
