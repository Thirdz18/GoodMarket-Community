
import os
import logging
from web3 import Web3
from eth_account import Account
from datetime import datetime

logger = logging.getLogger(__name__)

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

class CommunityStoriesBlockchain:
    def __init__(self):
        # Blockchain configuration
        self.celo_rpc_url = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
        self.chain_id = int(os.getenv('CHAIN_ID', 42220))
        self.gooddollar_contract = os.getenv('GOODDOLLAR_CONTRACT', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
        
        # Community Stories wallet key
        self.community_key = os.getenv('COMMUNITY_KEY')
        
        # Debug logging
        logger.info(f"🔍 Checking COMMUNITY_KEY configuration...")
        if self.community_key:
            logger.info(f"✅ COMMUNITY_KEY found (length: {len(self.community_key)})")
        else:
            logger.error("❌ COMMUNITY_KEY not found in environment variables")
        
        # Initialize Web3
        try:
            self.w3 = Web3(Web3.HTTPProvider(self.celo_rpc_url))
            if not self.w3.is_connected():
                logger.error("❌ Failed to connect to Celo network")
                self.enabled = False
            else:
                logger.info("✅ Connected to Celo network for Community Stories")
                self.enabled = True
        except Exception as e:
            logger.error(f"❌ Web3 initialization error: {e}")
            self.enabled = False
        
        # Load community wallet
        if self.community_key and self.enabled:
            try:
                if not self.community_key.startswith('0x'):
                    self.community_key = '0x' + self.community_key
                self.community_account = Account.from_key(self.community_key)
                logger.info(f"✅ Community Stories wallet loaded: {self.community_account.address[:8]}...")
                logger.info(f"💰 Ready to disburse Community Stories rewards!")
            except Exception as e:
                logger.error(f"❌ Error loading community wallet: {e}")
                logger.error(f"🔍 Please check if COMMUNITY_KEY is a valid private key")
                self.enabled = False
        else:
            if not self.community_key:
                logger.error("❌ COMMUNITY_KEY not configured in Secrets")
                logger.error("🔑 Please add COMMUNITY_KEY in Replit Secrets")
            self.enabled = False
    
    async def disburse_reward(self, recipient_wallet: str, amount: float, submission_id: str) -> dict:
        """Disburse Community Stories reward to user"""
        if not self.enabled:
            logger.error(f"❌ Community Stories blockchain service not enabled")
            logger.error(f"🔍 Check COMMUNITY_KEY in Secrets")
            return {
                'success': False,
                'error': 'Community Stories blockchain service not enabled',
                'error_type': 'service_disabled'
            }
        
        try:
            logger.info(f"💰 Disbursing {amount} G$ to {recipient_wallet[:8]}... for submission {submission_id}")
            
            # Check CELO balance for gas
            celo_balance = self.w3.eth.get_balance(self.community_account.address)
            celo_balance_formatted = celo_balance / (10 ** 18)
            min_celo_required = 0.01  # 0.01 CELO minimum
            
            if celo_balance_formatted < min_celo_required:
                logger.error(f"❌ Insufficient CELO for gas: {celo_balance_formatted} CELO < {min_celo_required} CELO")
                return {
                    'success': False,
                    'error': f'Community wallet needs CELO for gas. Current: {celo_balance_formatted:.4f} CELO. Please fund {self.community_account.address} with at least 0.01 CELO.',
                    'error_type': 'insufficient_gas'
                }
            
            # Validate recipient wallet
            if not recipient_wallet or not recipient_wallet.startswith('0x'):
                logger.error(f"❌ Invalid recipient wallet: {recipient_wallet}")
                return {
                    'success': False,
                    'error': 'Invalid recipient wallet address',
                    'error_type': 'invalid_wallet'
                }
            
            # Complete ERC20 ABI (balanceOf + transfer)
            erc20_abi = [
                {
                    "constant": True,
                    "inputs": [{"name": "_owner", "type": "address"}],
                    "name": "balanceOf",
                    "outputs": [{"name": "balance", "type": "uint256"}],
                    "type": "function"
                },
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
            
            # Create contract instance
            contract = self.w3.eth.contract(
                address=Web3.to_checksum_address(self.gooddollar_contract),
                abi=erc20_abi
            )
            
            # Convert amount to wei (18 decimals)
            amount_wei = int(amount * (10 ** 18))
            
            # Check balance
            balance = contract.functions.balanceOf(self.community_account.address).call()
            if balance < amount_wei:
                logger.error(f"❌ Insufficient balance: {balance / (10**18)} G$ < {amount} G$")
                return {
                    'success': False,
                    'error': 'Insufficient balance in community wallet',
                    'error_type': 'insufficient_balance'
                }
            
            # Build transaction
            tx = contract.functions.transfer(
                Web3.to_checksum_address(recipient_wallet),
                amount_wei
            ).build_transaction({
                'from': self.community_account.address,
                'gas': 250000,  # 250k gas limit for Community Stories rewards
                'gasPrice': self.w3.eth.gas_price,
                'nonce': self.w3.eth.get_transaction_count(self.community_account.address),
                'chainId': self.chain_id
            })
            
            # Sign transaction
            signed_tx = self.w3.eth.account.sign_transaction(tx, self.community_key)
            
            # Send transaction
            tx_hash = self.w3.eth.send_raw_transaction(signed_tx.raw_transaction)
            tx_hash_hex = tx_hash.hex()
            
            logger.info(f"✅ Transaction sent: {tx_hash_hex}")
            
            # Wait for confirmation
            receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
            
            if receipt.status == 1:
                logger.info(f"✅ Community Stories reward disbursed successfully!")
                return {
                    'success': True,
                    'tx_hash': tx_hash_hex,
                    'amount': amount,
                    'recipient': recipient_wallet,
                    'explorer_url': f'https://explorer.celo.org/mainnet/tx/{tx_hash_hex}'
                }
            else:
                # Try to decode exact revert reason via eth_call simulation
                revert_reason = "Unknown"
                try:
                    call_data = contract.functions.transfer(
                        Web3.to_checksum_address(recipient_wallet),
                        amount_wei
                    ).build_transaction({
                        'from': self.community_account.address,
                        'gas': 250000,
                        'gasPrice': self.w3.eth.gas_price,
                        'nonce': self.w3.eth.get_transaction_count(self.community_account.address),
                        'chainId': self.chain_id
                    })
                    self.w3.eth.call(call_data, receipt.blockNumber)
                except Exception as call_err:
                    if hasattr(call_err, 'data') and call_err.data:
                        raw = call_err.data
                        if isinstance(raw, str):
                            raw = bytes.fromhex(raw.replace('0x', ''))
                        revert_reason = _decode_revert_reason(raw)
                    else:
                        revert_reason = str(call_err)

                reason_lower = revert_reason.lower()
                if any(k in reason_lower for k in ['balance', 'insufficient', 'funds']):
                    error_type = "insufficient_balance"
                elif any(k in reason_lower for k in ['access', 'owner', 'authorized']):
                    error_type = "access_denied"
                else:
                    error_type = "contract_revert"

                logger.error(f"❌ Community Stories transaction failed [{error_type}]: {revert_reason} | TX: {tx_hash_hex}")
                return {
                    'success': False,
                    'error': f"Transaction failed: {revert_reason}",
                    'error_type': error_type,
                    'revert_reason': revert_reason,
                    'tx_hash': tx_hash_hex,
                    'explorer_url': f'https://explorer.celo.org/mainnet/tx/{tx_hash_hex}'
                }
                
        except Exception as e:
            logger.error(f"❌ Disbursement error: {e}")
            error_msg = str(e)
            
            # Check for specific error types
            if 'insufficient funds' in error_msg.lower():
                return {
                    'success': False,
                    'error': f'Insufficient CELO for gas fees. Please fund Community wallet {self.community_account.address} with at least 0.01 CELO.',
                    'error_type': 'insufficient_gas'
                }
            
            return {
                'success': False,
                'error': error_msg,
                'error_type': 'blockchain_error'
            }

# Global instance
community_stories_blockchain = CommunityStoriesBlockchain()
