"""
G$ Savings blockchain service.
All on-chain reads. Withdrawals and deposits happen directly from the user's wallet (frontend).

Contract mechanics (v2):
  - Min deposit: 1,000 G$ | Max: 10,000,000 G$
  - Lock durations: 1, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300, 330, 365 days
  - Tiered optional bonus (requires >= 150-day lock):
      10,000 –  99,999 G$  →  1,000 G$ bonus
     100,000 – 499,999 G$  →  2,500 G$ bonus
     500,000 – 10,000,000 G$ → 10,000 G$ bonus
  - Bonus only paid if reward pool has sufficient funds (optional / trustless)
  - No owner, no pause — fully decentralised savings vault
"""
import os
import logging
from web3 import Web3

logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))
SAVINGS_CONTRACT_ADDRESS = os.getenv('SAVINGS_CONTRACT_ADDRESS', '')
GD_TOKEN_ADDRESS = os.getenv('GOODDOLLAR_CONTRACT_ADDRESS', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')

SAVINGS_ABI = [
    # ── Constructor ──────────────────────────────────────────────────────
    {
        "inputs": [{"internalType": "address", "name": "_gd", "type": "address"}],
        "stateMutability": "nonpayable",
        "type": "constructor"
    },
    # ── Write functions ──────────────────────────────────────────────────
    {
        "inputs": [
            {"internalType": "uint256", "name": "amount", "type": "uint256"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"}
        ],
        "name": "depositSavings",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "depositId", "type": "uint256"}],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "fundRewardPool",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function"
    },
    # ── View: user data ──────────────────────────────────────────────────
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserDepositIds",
        "outputs": [{"internalType": "uint256[]", "name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function"
    },
    {
        "inputs": [{"internalType": "uint256", "name": "depositId", "type": "uint256"}],
        "name": "getDeposit",
        "outputs": [
            {"internalType": "address",  "name": "owner_",       "type": "address"},
            {"internalType": "uint256",  "name": "amount",        "type": "uint256"},
            {"internalType": "uint256",  "name": "lockDays",      "type": "uint256"},
            {"internalType": "uint256",  "name": "depositedAt",   "type": "uint256"},
            {"internalType": "uint256",  "name": "unlocksAt",     "type": "uint256"},
            {"internalType": "bool",     "name": "withdrawn",     "type": "bool"},
            {"internalType": "bool",     "name": "bonusClaimed",  "type": "bool"},
            {"internalType": "bool",     "name": "isUnlocked",    "type": "bool"},
            {"internalType": "bool",     "name": "bonusEligible", "type": "bool"},
            {"internalType": "uint256",  "name": "pendingBonus",  "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    # ── View: contract stats ─────────────────────────────────────────────
    {
        "inputs": [],
        "name": "getContractStats",
        "outputs": [
            {"internalType": "uint256", "name": "totalLocked",      "type": "uint256"},
            {"internalType": "uint256", "name": "rewardPoolBalance","type": "uint256"},
            {"internalType": "uint256", "name": "contractBalance",  "type": "uint256"},
            {"internalType": "uint256", "name": "totalDeposits",    "type": "uint256"}
        ],
        "stateMutability": "view",
        "type": "function"
    },
    # ── View: bonus calculator ───────────────────────────────────────────
    {
        "inputs": [
            {"internalType": "uint256", "name": "amount",   "type": "uint256"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"}
        ],
        "name": "getBonusAmount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "pure",
        "type": "function"
    },
    # ── View: constants ──────────────────────────────────────────────────
    {
        "inputs": [], "name": "MIN_DEPOSIT",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "MAX_DEPOSIT",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_SHORT_DAYS",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_SHORT_AMOUNT",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_MIN_DAYS",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_TIER1_MIN",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_TIER1_AMOUNT",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_TIER2_MIN",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_TIER2_AMOUNT",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_TIER3_MIN",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "BONUS_TIER3_AMOUNT",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "depositIdCounter",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "rewardPool",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view", "type": "function"
    },
    {
        "inputs": [], "name": "getValidDurations",
        "outputs": [{"internalType": "uint16[13]", "name": "", "type": "uint16[13]"}],
        "stateMutability": "view", "type": "function"
    },
]

ERC20_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
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
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount", "type": "uint256"}
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function"
    },
]


def get_w3():
    return Web3(Web3.HTTPProvider(CELO_RPC_URL))


def get_savings_contract(w3):
    if not SAVINGS_CONTRACT_ADDRESS:
        raise ValueError("SAVINGS_CONTRACT_ADDRESS not set")
    return w3.eth.contract(
        address=Web3.to_checksum_address(SAVINGS_CONTRACT_ADDRESS),
        abi=SAVINGS_ABI
    )


def get_gd_contract(w3):
    return w3.eth.contract(
        address=Web3.to_checksum_address(GD_TOKEN_ADDRESS),
        abi=ERC20_ABI
    )


def get_contract_stats():
    """Return high-level stats about the savings vault."""
    try:
        w3 = get_w3()
        contract = get_savings_contract(w3)
        stats = contract.functions.getContractStats().call()
        return {
            "total_locked":        str(stats[0]),
            "total_locked_gd":     float(Web3.from_wei(stats[0], 'ether')),
            "reward_pool":         str(stats[1]),
            "reward_pool_gd":      float(Web3.from_wei(stats[1], 'ether')),
            "contract_balance":    str(stats[2]),
            "total_deposits_count": stats[3],
            "contract_address":    SAVINGS_CONTRACT_ADDRESS,
        }
    except Exception as e:
        logger.error(f"get_contract_stats error: {e}")
        return None


def get_user_deposits(wallet_address):
    """Return all deposits for a given wallet address."""
    try:
        w3 = get_w3()
        contract = get_savings_contract(w3)
        addr = Web3.to_checksum_address(wallet_address)
        ids = contract.functions.getUserDepositIds(addr).call()

        result = []
        for dep_id in ids:
            try:
                d = contract.functions.getDeposit(dep_id).call()
                result.append({
                    "id":            dep_id,
                    "owner":         d[0],
                    "amount":        str(d[1]),
                    "amount_gd":     float(Web3.from_wei(d[1], 'ether')),
                    "lock_days":     d[2],
                    "deposited_at":  d[3],
                    "unlocks_at":    d[4],
                    "withdrawn":     d[5],
                    "bonus_claimed": d[6],
                    "is_unlocked":   d[7],
                    "bonus_eligible": d[8],
                    "pending_bonus": str(d[9]),
                    "pending_bonus_gd": float(Web3.from_wei(d[9], 'ether')),
                })
            except Exception as inner_e:
                logger.warning(f"Error fetching deposit {dep_id}: {inner_e}")

        return result
    except Exception as e:
        logger.error(f"get_user_deposits error: {e}")
        return []


def get_gd_allowance(wallet_address):
    """Check how much G$ the user has approved for the savings contract."""
    try:
        w3 = get_w3()
        gd = get_gd_contract(w3)
        addr = Web3.to_checksum_address(wallet_address)
        savings_addr = Web3.to_checksum_address(SAVINGS_CONTRACT_ADDRESS)
        return gd.functions.allowance(addr, savings_addr).call()
    except Exception as e:
        logger.error(f"get_gd_allowance error: {e}")
        return 0
