"""
G$ Savings blockchain service.
All on-chain reads. Withdrawals and deposits happen directly from the user's wallet (frontend).

Contract mechanics (v5 — multi-token, slot-based, custom-duration bonuses):
  - Tokens accepted: G$, CELO, cUSD, USDT (Tether on Celo, 6 decimals).
  - One slot per (user, token, lockDays). Top-ups inherit the slot's
    original unlocksAt (no lock extension).
  - Lock duration: ANY integer day from 1 to 360 (inclusive). No fixed
    preset durations — the user types a custom number of days.
  - Per-token min/max (using each token's NATIVE decimals):
      G$:   1,000        – 10,000,000   (18 decimals)
      CELO: 1            – 100,000      (18 decimals)
      cUSD: 1            – 1,000,000    (18 decimals)
      USDT: 1            – 1,000,000    ( 6 decimals)
  - Per-duration bonus structure (always paid in G$, regardless of
    deposit token; internal contract ratio 1 G$ ≡ 0.001 CELO ≡ 0.001 cUSD ≡
    0.001 USDT):
      1..29-day   → 30 G$        if amount ≥ per-token MIN.
      30..360-day → (lockDays * 500 / 30) G$ if amount ≥ per-token
                     "100k G$ equivalent" (G$ 100,000 / CELO 100 /
                     cUSD 100 / USDT 100). 30d → 500 G$, 60d → 1,000 G$,
                     ..., 360d → 6,000 G$.
      ≥300-day with amount ≥ per-token "1M G$ equivalent"
         (G$ 1,000,000 / CELO 1,000 / cUSD 1,000 / USDT 1,000) REPLACES
         the mid-tier value with a flat 20,000 G$ loyalty bonus.
  - Bonus only paid if reward pool has sufficient G$ (optional / trustless).
  - No owner, no pause, no early withdrawal.

Legacy contracts (read-only):
  - v4 (multi-token, fixed durations [1, 30, ..., 365]). Was the live
    contract before v5; users with active v4 saves can still see and
    withdraw them via the legacy v4 panel.
  - v2 (single-token G$ only, deposit-id based). Frozen permanently.
"""
import os
import logging
from web3 import Web3

logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))
SAVINGS_CONTRACT_ADDRESS = os.getenv('SAVINGS_CONTRACT_ADDRESS', '')
GD_TOKEN_ADDRESS = os.getenv('GOODDOLLAR_CONTRACT_ADDRESS', '0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A')
CELO_TOKEN_ADDRESS = os.getenv('CELO_TOKEN_ADDRESS', '0x471EcE3750Da237f93B8E339c536989b8978a438')
CUSD_TOKEN_ADDRESS = os.getenv('CUSD_TOKEN_ADDRESS', '0x765DE816845861e75A25fCA122bb6898B8B1282a')
# Tether (USD₮) on Celo — 6-decimal ERC-20, not the 18-decimal pattern.
USDT_TOKEN_ADDRESS = os.getenv('USDT_TOKEN_ADDRESS', '0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e')

# Legacy v4 contract — multi-token (G$/CELO/cUSD) savings vault that used
# fixed preset durations. Kept read-only so users with active v4 slots can
# still see and withdraw them in the UI after the v5 redeploy.
LEGACY_V4_CONTRACT_ADDRESS = os.getenv(
    'LEGACY_V4_CONTRACT_ADDRESS',
    '0x78d2a6Dd976337d3bEaFA0c30df6a0fDE949a618',
)

# Legacy v2 contract — frozen-in-place forever, read-only support so users with
# old (single-token, deposit-id-based) saves can still see and withdraw them.
LEGACY_V2_CONTRACT_ADDRESS = '0xF3cca43F5C108d3dEf01Ff1E138866aC1ed00e9c'

# Map of supported tokens, used by the frontend / API to label slots.
# USDT uses 6 decimals; all others are 18. Anywhere we convert raw on-chain
# amounts to human-readable values we must scale by the token's own decimals
# (Web3.from_wei(_, 'ether') would over-divide a USDT balance by 1e12).
SUPPORTED_TOKENS = {
    GD_TOKEN_ADDRESS.lower():   {"symbol": "G$",   "decimals": 18},
    CELO_TOKEN_ADDRESS.lower(): {"symbol": "CELO", "decimals": 18},
    CUSD_TOKEN_ADDRESS.lower(): {"symbol": "cUSD", "decimals": 18},
    USDT_TOKEN_ADDRESS.lower(): {"symbol": "USDT", "decimals":  6},
}


def _token_meta(addr):
    if not addr:
        return {"symbol": "?", "decimals": 18}
    return SUPPORTED_TOKENS.get(addr.lower(), {"symbol": "?", "decimals": 18})


def _raw_to_human(raw, decimals):
    """Scale a raw on-chain integer amount to its human-readable float using
    the token's native decimals. Returns 0.0 on any conversion error so the
    UI never crashes on a malformed value."""
    try:
        d = int(decimals) if decimals is not None else 18
        if d < 0:
            d = 18
        return float(int(raw)) / float(10 ** d)
    except Exception:
        return 0.0


# Common slot-detail tuple shared between v4 and v5 ABIs.
_USER_ACTIVE_SLOTS_OUT = [
    {"internalType": "address[]", "name": "tokens",         "type": "address[]"},
    {"internalType": "uint256[]", "name": "lockDays_",      "type": "uint256[]"},
    {"internalType": "uint256[]", "name": "amounts",        "type": "uint256[]"},
    {"internalType": "uint256[]", "name": "unlocksAts",     "type": "uint256[]"},
    {"internalType": "bool[]",    "name": "areUnlocked",    "type": "bool[]"},
    {"internalType": "bool[]",    "name": "bonusClaimed",   "type": "bool[]"},
    {"internalType": "uint256[]", "name": "pendingBonuses", "type": "uint256[]"},
]

SAVINGS_ABI = [
    # ── Constructor (v5 — 4-token registry) ─────────────────────────────────
    {
        "inputs": [
            {"internalType": "address", "name": "_gd",        "type": "address"},
            {"internalType": "address", "name": "_celoToken", "type": "address"},
            {"internalType": "address", "name": "_cusd",      "type": "address"},
            {"internalType": "address", "name": "_usdt",      "type": "address"},
        ],
        "stateMutability": "nonpayable",
        "type": "constructor",
    },
    # ── Write functions ──────────────────────────────────────────────────
    {
        "inputs": [
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "amount",   "type": "uint256"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
        ],
        "name": "depositSavings",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
        ],
        "name": "withdraw",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "amount", "type": "uint256"}],
        "name": "fundRewardPool",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    },
    # ── View: slot details ───────────────────────────────────────────────
    {
        "inputs": [
            {"internalType": "address", "name": "user",     "type": "address"},
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
        ],
        "name": "getSlot",
        "outputs": [
            {"internalType": "uint256", "name": "amount",         "type": "uint256"},
            {"internalType": "uint256", "name": "firstDepositAt", "type": "uint256"},
            {"internalType": "uint256", "name": "unlocksAt",      "type": "uint256"},
            {"internalType": "bool",    "name": "bonusClaimed",   "type": "bool"},
            {"internalType": "bool",    "name": "isUnlocked",     "type": "bool"},
            {"internalType": "uint256", "name": "pendingBonus",   "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserSlotRefs",
        "outputs": [
            {
                "components": [
                    {"internalType": "address", "name": "token",    "type": "address"},
                    {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
                ],
                "internalType": "struct GDSavings.SlotRef[]",
                "name": "",
                "type": "tuple[]",
            }
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserActiveSlots",
        "outputs": _USER_ACTIVE_SLOTS_OUT,
        "stateMutability": "view",
        "type": "function",
    },
    # ── View: contract stats (v5 — USDT added) ──────────────────────────
    {
        "inputs": [],
        "name": "getContractStats",
        "outputs": [
            {"internalType": "uint256", "name": "totalLockedGd",       "type": "uint256"},
            {"internalType": "uint256", "name": "totalLockedCelo",     "type": "uint256"},
            {"internalType": "uint256", "name": "totalLockedCusd",     "type": "uint256"},
            {"internalType": "uint256", "name": "totalLockedUsdt",     "type": "uint256"},
            {"internalType": "uint256", "name": "rewardPoolBalance",   "type": "uint256"},
            {"internalType": "uint256", "name": "contractGdBalance",   "type": "uint256"},
            {"internalType": "uint256", "name": "contractCeloBalance", "type": "uint256"},
            {"internalType": "uint256", "name": "contractCusdBalance", "type": "uint256"},
            {"internalType": "uint256", "name": "contractUsdtBalance", "type": "uint256"},
            {"internalType": "uint256", "name": "slotsOpenedTotal",    "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # ── View: bonus calculator ───────────────────────────────────────────
    {
        "inputs": [
            {"internalType": "address", "name": "token",    "type": "address"},
            {"internalType": "uint256", "name": "amount",   "type": "uint256"},
            {"internalType": "uint256", "name": "lockDays", "type": "uint256"},
        ],
        "name": "getBonusAmount",
        "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "token", "type": "address"}],
        "name": "getMinMax",
        "outputs": [
            {"internalType": "uint256", "name": "minA", "type": "uint256"},
            {"internalType": "uint256", "name": "maxA", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "address", "name": "token", "type": "address"}],
        "name": "isAllowedToken",
        "outputs": [{"internalType": "bool", "name": "", "type": "bool"}],
        "stateMutability": "view",
        "type": "function",
    },
    # v5: continuous duration range, not a fixed [1, 30, ..., 365] preset list.
    {
        "inputs": [],
        "name": "getDurationRange",
        "outputs": [
            {"internalType": "uint256", "name": "minDays", "type": "uint256"},
            {"internalType": "uint256", "name": "maxDays", "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [],
        "name": "getTokens",
        "outputs": [
            {"internalType": "address", "name": "gdAddr",   "type": "address"},
            {"internalType": "address", "name": "celoAddr", "type": "address"},
            {"internalType": "address", "name": "cusdAddr", "type": "address"},
            {"internalType": "address", "name": "usdtAddr", "type": "address"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
    # ── View: state vars ─────────────────────────────────────────────────
    {"inputs": [], "name": "rewardPool",
     "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "totalSlotsOpened",
     "outputs": [{"internalType": "uint256", "name": "", "type": "uint256"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "gd",
     "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "celoToken",
     "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "cusd",
     "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
    {"inputs": [], "name": "usdt",
     "outputs": [{"internalType": "address", "name": "", "type": "address"}],
     "stateMutability": "view", "type": "function"},
]

# Legacy v4 ABI — only the read functions used by the legacy v4 panel.
# v4 used fixed [1, 30, 60, ..., 365] durations and 3 tokens (G$/CELO/cUSD).
# Withdrawals from v4 are signed directly by the user's wallet on the
# frontend using the matching JS ABI, so we only need the reads here.
LEGACY_V4_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserActiveSlots",
        "outputs": _USER_ACTIVE_SLOTS_OUT,
        "stateMutability": "view",
        "type": "function",
    },
]

# Legacy v2 ABI — only the read functions we need to list a user's old deposits.
# Withdrawals from the v2 contract are signed by the user's wallet on the
# frontend (using the same v2 ABI hardcoded in templates/savings.html), so this
# backend-side ABI does not need to include the `withdraw(uint256)` mutation.
LEGACY_V2_ABI = [
    {
        "inputs": [{"internalType": "address", "name": "user", "type": "address"}],
        "name": "getUserDepositIds",
        "outputs": [{"internalType": "uint256[]", "name": "", "type": "uint256[]"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [{"internalType": "uint256", "name": "depositId", "type": "uint256"}],
        "name": "getDeposit",
        "outputs": [
            {"internalType": "address", "name": "owner_",        "type": "address"},
            {"internalType": "uint256", "name": "amount",        "type": "uint256"},
            {"internalType": "uint256", "name": "lockDays",      "type": "uint256"},
            {"internalType": "uint256", "name": "depositedAt",   "type": "uint256"},
            {"internalType": "uint256", "name": "unlocksAt",     "type": "uint256"},
            {"internalType": "bool",    "name": "withdrawn",     "type": "bool"},
            {"internalType": "bool",    "name": "bonusClaimed",  "type": "bool"},
            {"internalType": "bool",    "name": "isUnlocked",    "type": "bool"},
            {"internalType": "bool",    "name": "bonusEligible", "type": "bool"},
            {"internalType": "uint256", "name": "pendingBonus",  "type": "uint256"},
        ],
        "stateMutability": "view",
        "type": "function",
    },
]

ERC20_ABI = [
    {
        "inputs": [{"name": "owner", "type": "address"}],
        "name": "balanceOf",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "owner",   "type": "address"},
            {"name": "spender", "type": "address"},
        ],
        "name": "allowance",
        "outputs": [{"name": "", "type": "uint256"}],
        "stateMutability": "view",
        "type": "function",
    },
    {
        "inputs": [
            {"name": "spender", "type": "address"},
            {"name": "amount",  "type": "uint256"},
        ],
        "name": "approve",
        "outputs": [{"name": "", "type": "bool"}],
        "stateMutability": "nonpayable",
        "type": "function",
    },
]


def get_w3():
    return Web3(Web3.HTTPProvider(CELO_RPC_URL))


def get_savings_contract(w3):
    if not SAVINGS_CONTRACT_ADDRESS:
        raise ValueError("SAVINGS_CONTRACT_ADDRESS not set")
    return w3.eth.contract(
        address=Web3.to_checksum_address(SAVINGS_CONTRACT_ADDRESS),
        abi=SAVINGS_ABI,
    )


def get_erc20_contract(w3, token_address):
    return w3.eth.contract(
        address=Web3.to_checksum_address(token_address),
        abi=ERC20_ABI,
    )


def get_gd_contract(w3):
    """Backwards-compatible helper for callers that only need the G$ token."""
    return get_erc20_contract(w3, GD_TOKEN_ADDRESS)


def get_contract_stats():
    """Return high-level stats about the v5 savings vault.

    USDT uses 6 decimals so we must scale its raw values with the token's
    own decimals — calling Web3.from_wei(_, 'ether') on a USDT amount would
    under-report it by a factor of 10¹².
    """
    try:
        w3 = get_w3()
        contract = get_savings_contract(w3)
        s = contract.functions.getContractStats().call()
        (
            total_locked_gd_raw,
            total_locked_celo_raw,
            total_locked_cusd_raw,
            total_locked_usdt_raw,
            reward_pool_raw,
            contract_gd_raw,
            contract_celo_raw,
            contract_cusd_raw,
            contract_usdt_raw,
            slots_opened,
        ) = s
        usdt_decimals = _token_meta(USDT_TOKEN_ADDRESS)["decimals"]
        return {
            "total_locked_gd":       str(total_locked_gd_raw),
            "total_locked_gd_h":     _raw_to_human(total_locked_gd_raw,   18),
            "total_locked_celo":     str(total_locked_celo_raw),
            "total_locked_celo_h":   _raw_to_human(total_locked_celo_raw, 18),
            "total_locked_cusd":     str(total_locked_cusd_raw),
            "total_locked_cusd_h":   _raw_to_human(total_locked_cusd_raw, 18),
            "total_locked_usdt":     str(total_locked_usdt_raw),
            "total_locked_usdt_h":   _raw_to_human(total_locked_usdt_raw, usdt_decimals),
            "reward_pool":           str(reward_pool_raw),
            "reward_pool_gd":        _raw_to_human(reward_pool_raw, 18),
            "contract_gd_balance":   str(contract_gd_raw),
            "contract_celo_balance": str(contract_celo_raw),
            "contract_cusd_balance": str(contract_cusd_raw),
            "contract_usdt_balance": str(contract_usdt_raw),
            "total_slots_opened":    slots_opened,
            "contract_address":      SAVINGS_CONTRACT_ADDRESS,
            "tokens": {
                "gd":   GD_TOKEN_ADDRESS,
                "celo": CELO_TOKEN_ADDRESS,
                "cusd": CUSD_TOKEN_ADDRESS,
                "usdt": USDT_TOKEN_ADDRESS,
            },
        }
    except Exception as e:
        logger.error(f"get_contract_stats error: {e}")
        return None


def _normalize_active_slots(raw_slots):
    """Shared helper for v4 + v5 getUserActiveSlots() responses."""
    (
        tokens,
        lock_days_list,
        amounts,
        unlocks_ats,
        are_unlocked,
        bonus_claimeds,
        pending_bonuses,
    ) = raw_slots

    result = []
    for i in range(len(tokens)):
        token_addr = tokens[i]
        meta = _token_meta(token_addr)
        decimals = meta["decimals"]
        result.append({
            "token":             token_addr,
            "token_symbol":      meta["symbol"],
            "token_decimals":    decimals,
            "lock_days":         int(lock_days_list[i]),
            "amount":            str(amounts[i]),
            "amount_h":          _raw_to_human(amounts[i], decimals),
            "unlocks_at":        int(unlocks_ats[i]),
            "is_unlocked":       bool(are_unlocked[i]),
            "bonus_claimed":     bool(bonus_claimeds[i]),
            "pending_bonus":     str(pending_bonuses[i]),
            # Pending bonus is always paid in G$ (18-decimal) on both v4
            # and v5, regardless of the deposit token.
            "pending_bonus_gd":  _raw_to_human(pending_bonuses[i], 18),
        })
    return result


def get_user_deposits(wallet_address):
    """Return all active slots for a given wallet address.

    Each entry represents one (token, lockDays) slot with its current
    aggregated `amount` and the slot's `unlocks_at` (which never moves
    after the first deposit, even if the user tops up later).
    """
    try:
        w3 = get_w3()
        contract = get_savings_contract(w3)
        addr = Web3.to_checksum_address(wallet_address)
        raw_slots = contract.functions.getUserActiveSlots(addr).call()
        return _normalize_active_slots(raw_slots)
    except Exception as e:
        logger.error(f"get_user_deposits error: {e}")
        return []


def get_token_allowance(wallet_address, token_address):
    """Check how much `token_address` the user has approved for the savings contract."""
    try:
        w3 = get_w3()
        token = get_erc20_contract(w3, token_address)
        addr = Web3.to_checksum_address(wallet_address)
        savings_addr = Web3.to_checksum_address(SAVINGS_CONTRACT_ADDRESS)
        return token.functions.allowance(addr, savings_addr).call()
    except Exception as e:
        logger.error(f"get_token_allowance({token_address}) error: {e}")
        return 0


def get_gd_allowance(wallet_address):
    """Backwards-compatible: G$ allowance for the savings contract."""
    return get_token_allowance(wallet_address, GD_TOKEN_ADDRESS)


def get_user_token_balances(wallet_address):
    """Return the user's balances + savings-vault allowances for all
    supported tokens, scaled by each token's own decimals."""
    try:
        w3 = get_w3()
        addr = Web3.to_checksum_address(wallet_address)
        out = {}
        token_map = (
            ("gd",   GD_TOKEN_ADDRESS),
            ("celo", CELO_TOKEN_ADDRESS),
            ("cusd", CUSD_TOKEN_ADDRESS),
            ("usdt", USDT_TOKEN_ADDRESS),
        )
        for key, token_addr in token_map:
            decimals = _token_meta(token_addr)["decimals"]
            try:
                token = get_erc20_contract(w3, token_addr)
                bal = token.functions.balanceOf(addr).call()
                allowance = (
                    token.functions.allowance(
                        addr, Web3.to_checksum_address(SAVINGS_CONTRACT_ADDRESS)
                    ).call()
                    if SAVINGS_CONTRACT_ADDRESS
                    else 0
                )
                out[key] = {
                    "address":     token_addr,
                    "decimals":    decimals,
                    "balance":     str(bal),
                    "balance_h":   _raw_to_human(bal, decimals),
                    "allowance":   str(allowance),
                    "allowance_h": _raw_to_human(allowance, decimals),
                }
            except Exception as inner:
                logger.warning(f"balance fetch failed for {key}: {inner}")
                out[key] = {
                    "address":     token_addr,
                    "decimals":    decimals,
                    "balance":     "0",
                    "balance_h":   0.0,
                    "allowance":   "0",
                    "allowance_h": 0.0,
                }
        return out
    except Exception as e:
        logger.error(f"get_user_token_balances error: {e}")
        return {}


def get_legacy_contract(w3):
    """The frozen v2 contract (single-token, deposit-id based). Read-only here."""
    return w3.eth.contract(
        address=Web3.to_checksum_address(LEGACY_V2_CONTRACT_ADDRESS),
        abi=LEGACY_V2_ABI,
    )


def get_legacy_v4_contract(w3):
    """The v4 multi-token savings contract — read-only after the v5 redeploy.
    Users with active v4 slots can still withdraw them from the frontend."""
    return w3.eth.contract(
        address=Web3.to_checksum_address(LEGACY_V4_CONTRACT_ADDRESS),
        abi=LEGACY_V4_ABI,
    )


def get_user_legacy_v4_deposits(wallet_address):
    """Return all active v4 slots for the given wallet.

    Same shape as `get_user_deposits` (active-only), so the frontend can
    reuse the same row-rendering logic for the legacy v4 panel. Returns an
    empty list if the wallet never opened a v4 slot or the contract call
    fails (e.g. v4 contract address not configured).
    """
    if not LEGACY_V4_CONTRACT_ADDRESS:
        return []
    try:
        w3 = get_w3()
        contract = get_legacy_v4_contract(w3)
        addr = Web3.to_checksum_address(wallet_address)
        raw_slots = contract.functions.getUserActiveSlots(addr).call()
        return _normalize_active_slots(raw_slots)
    except Exception as e:
        logger.error(f"get_user_legacy_v4_deposits error: {e}")
        return []


def get_user_legacy_deposits(wallet_address):
    """Return all v2 deposits (old contract) for the given wallet.

    Each entry uses the v2 schema: id, amount (G$ wei), lock_days,
    deposited_at, unlocks_at, withdrawn, bonus_claimed, is_unlocked,
    bonus_eligible, pending_bonus_gd. The frontend renders these in a
    separate, collapsible "Legacy Saves" panel; users can withdraw them
    by signing `withdraw(depositId)` directly to the v2 contract.
    """
    try:
        w3 = get_w3()
        legacy = get_legacy_contract(w3)
        addr = Web3.to_checksum_address(wallet_address)
        ids = legacy.functions.getUserDepositIds(addr).call()
        result = []
        for dep_id in ids:
            try:
                (
                    _owner,
                    amount_raw,
                    lock_days,
                    deposited_at,
                    unlocks_at,
                    withdrawn,
                    bonus_claimed,
                    is_unlocked,
                    bonus_eligible,
                    pending_bonus_raw,
                ) = legacy.functions.getDeposit(int(dep_id)).call()
            except Exception as inner:
                logger.warning(f"legacy getDeposit({dep_id}) failed: {inner}")
                continue
            result.append({
                "id":               int(dep_id),
                "amount":           str(amount_raw),
                "amount_gd":        float(Web3.from_wei(amount_raw, 'ether')),
                "lock_days":        int(lock_days),
                "deposited_at":     int(deposited_at),
                "unlocks_at":       int(unlocks_at),
                "withdrawn":        bool(withdrawn),
                "bonus_claimed":    bool(bonus_claimed),
                "is_unlocked":      bool(is_unlocked),
                "bonus_eligible":   bool(bonus_eligible),
                "pending_bonus":    str(pending_bonus_raw),
                "pending_bonus_gd": float(Web3.from_wei(pending_bonus_raw, 'ether')),
            })
        return result
    except Exception as e:
        logger.error(f"get_user_legacy_deposits error: {e}")
        return []
