// SPDX-License-Identifier: MIT
pragma solidity ^0.8.21;

/**
 * GDSavings v5 — Multi-token, slot-based, custom-duration savings vault.
 *
 * Tokens supported on Celo Mainnet:
 *   - G$   (GoodDollar)           — 18 decimals
 *   - CELO                        — 18 decimals
 *   - cUSD                        — 18 decimals
 *   - USDT (Tether on Celo)       —  6 decimals
 *
 * Mechanics:
 *   - One slot per (user, token, lockDays). Top-ups into an existing slot
 *     KEEP the original unlocksAt — adding to an existing save does NOT
 *     extend the lock period. All deposits in the slot unlock together.
 *   - Lock duration: ANY integer day from 1 to 360 (inclusive). The user
 *     types a custom number of days; there are no preset durations.
 *   - Per-token min/max (using each token's native decimals):
 *       G$:   1,000        – 10,000,000   (18d)
 *       CELO: 1            – 100,000      (18d)
 *       cUSD: 1            – 1,000,000    (18d)
 *       USDT: 1            – 1,000,000    ( 6d)
 *   - No early withdrawal. Withdrawal only after slot.unlocksAt.
 *   - On mature withdrawal the user receives 100% of principal in the
 *     deposit token, plus a G$ bonus (if eligible AND reward pool funded).
 *   - Bonus tiers (always paid in G$ regardless of deposit token):
 *       1–29-day "tester" lock, amount ≥ per-token MIN → 30 G$.
 *       30–360-day "mid-tier" lock, amount ≥ per-token "100k G$ eq."
 *           → (lockDays * 500 / 30) G$  (i.e. 30d → 500 G$, 60d → 1,000 G$,
 *             …, 360d → 6,000 G$).
 *       ≥300-day "loyalty" lock, amount ≥ per-token "1M G$ eq."
 *           → 20,000 G$ (replaces the mid-tier value when eligible).
 *   - Anyone can fund the G$ reward pool via fundRewardPool(); funds added
 *     are non-withdrawable by anyone (used exclusively for bonuses).
 *   - No owner, no admin, no pause, no emergency, no early withdrawal.
 */

interface IERC20 {
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    function totalSupply() external view returns (uint256);
    function balanceOf(address account) external view returns (uint256);
    function transfer(address to, uint256 amount) external returns (bool);
    function allowance(address owner, address spender) external view returns (uint256);
    function approve(address spender, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
}

/// @dev Minimal SafeERC20-style helper. Tolerates non-standard tokens that
///      return no value from transfer/transferFrom (e.g. USDT on some chains).
library SafeERC20 {
    function safeTransfer(IERC20 token, address to, uint256 value) internal {
        _callOptionalReturn(token, abi.encodeWithSelector(token.transfer.selector, to, value));
    }

    function safeTransferFrom(IERC20 token, address from, address to, uint256 value) internal {
        _callOptionalReturn(token, abi.encodeWithSelector(token.transferFrom.selector, from, to, value));
    }

    function _callOptionalReturn(IERC20 token, bytes memory data) private {
        require(address(token).code.length > 0, "SafeERC20: token has no code");
        (bool success, bytes memory returndata) = address(token).call(data);
        require(success, "SafeERC20: low-level call failed");
        if (returndata.length > 0) {
            require(abi.decode(returndata, (bool)), "SafeERC20: ERC20 op did not succeed");
        }
    }
}

abstract contract ReentrancyGuard {
    uint256 private constant _NOT_ENTERED = 1;
    uint256 private constant _ENTERED = 2;
    uint256 private _status;

    constructor() {
        _status = _NOT_ENTERED;
    }

    modifier nonReentrant() {
        require(_status != _ENTERED, "ReentrancyGuard: reentrant call");
        _status = _ENTERED;
        _;
        _status = _NOT_ENTERED;
    }
}

contract GDSavings is ReentrancyGuard {
    using SafeERC20 for IERC20;

    // ── Token registry (immutable) ──────────────────────────────────────────
    address public immutable gd;
    address public immutable celoToken;
    address public immutable cusd;
    address public immutable usdt;

    // ── Per-token min/max (in each token's native decimals) ─────────────────
    uint256 public constant MIN_DEPOSIT_GD   = 1_000      * 1e18;
    uint256 public constant MAX_DEPOSIT_GD   = 10_000_000 * 1e18;
    uint256 public constant MIN_DEPOSIT_CELO = 1          * 1e18;
    uint256 public constant MAX_DEPOSIT_CELO = 100_000    * 1e18;
    uint256 public constant MIN_DEPOSIT_CUSD = 1          * 1e18;
    uint256 public constant MAX_DEPOSIT_CUSD = 1_000_000  * 1e18;
    // USDT on Celo uses 6 decimals.
    uint256 public constant MIN_DEPOSIT_USDT = 1          * 1e6;
    uint256 public constant MAX_DEPOSIT_USDT = 1_000_000  * 1e6;

    // ── Bonus rules (rewards always denominated in G$) ──────────────────────
    // Per-duration bonus structure (v5):
    //   1..29-day   → 30 G$ if amount >= per-token MIN.
    //   30..360-day → (lockDays * 500 / 30) G$ if amount >= per-token
    //                 "100k G$ equivalent" (mid-tier).
    //   >=300-day with amount >= per-token "1M G$ equivalent" REPLACES the
    //   mid-tier value with a flat 20,000 G$ "loyalty" bonus.
    uint256 public constant BONUS_TESTER       =     30 * 1e18;
    uint256 public constant BONUS_PER_30D_STEP =    500 * 1e18; // mid-tier step value
    uint256 public constant BONUS_LOYALTY      = 20_000 * 1e18;

    // Per-token "100k G$ equivalent" thresholds for mid-tier bonus.
    // Internal contract ratio: 1 G$ ≡ 0.001 CELO ≡ 0.001 cUSD ≡ 0.001 USDT.
    uint256 public constant MID_TIER_MIN_GD   = 100_000 * 1e18;
    uint256 public constant MID_TIER_MIN_CELO =     100 * 1e18;
    uint256 public constant MID_TIER_MIN_CUSD =     100 * 1e18;
    uint256 public constant MID_TIER_MIN_USDT =     100 * 1e6;

    // Per-token "1M G$ equivalent" thresholds for loyalty bonus.
    uint256 public constant LONG_TIER_MIN_GD   = 1_000_000 * 1e18;
    uint256 public constant LONG_TIER_MIN_CELO =     1_000 * 1e18;
    uint256 public constant LONG_TIER_MIN_CUSD =     1_000 * 1e18;
    uint256 public constant LONG_TIER_MIN_USDT =     1_000 * 1e6;

    // ── Lock duration range (inclusive) ─────────────────────────────────────
    uint256 public constant MIN_LOCK_DAYS = 1;
    uint256 public constant MAX_LOCK_DAYS = 360;
    uint256 public constant LOYALTY_MIN_DAYS = 300;

    // ── G$ reward pool ──────────────────────────────────────────────────────
    /// @notice G$ funded by sponsors for bonus payouts. Non-withdrawable.
    uint256 public rewardPool;

    // ── Slot model ──────────────────────────────────────────────────────────
    struct DepositSlot {
        uint256 amount;
        uint256 firstDepositAt;
        uint256 unlocksAt;
        bool    bonusClaimed;
    }

    /// @notice slots[user][token][lockDays] => DepositSlot
    mapping(address => mapping(address => mapping(uint256 => DepositSlot))) public slots;

    struct SlotRef {
        address token;
        uint256 lockDays;
    }

    /// @notice History of every (token, lockDays) the user has ever opened.
    ///         May contain inactive entries (slot.amount == 0); filter with
    ///         getUserActiveSlots() for current active-only view.
    mapping(address => SlotRef[]) private _userSlotRefs;
    mapping(address => mapping(address => mapping(uint256 => bool))) private _userSlotKnown;

    /// @notice Aggregate count of slot openings ever (for stats).
    uint256 public totalSlotsOpened;

    // ── Events ──────────────────────────────────────────────────────────────
    event Saved(
        address indexed user,
        address indexed token,
        uint256 indexed lockDays,
        uint256 amountAdded,
        uint256 newSlotTotal,
        uint256 unlocksAt,
        bool    isTopUp
    );

    event Withdrawn(
        address indexed user,
        address indexed token,
        uint256 indexed lockDays,
        uint256 principal,
        uint256 timestamp
    );

    event BonusPaid(
        address indexed user,
        address indexed token,
        uint256 indexed lockDays,
        uint256 bonusGd,
        uint256 timestamp
    );

    event RewardPoolFunded(
        address indexed sponsor,
        uint256 amount,
        uint256 timestamp
    );

    // ── Constructor ─────────────────────────────────────────────────────────
    /**
     * @param _gd        Address of the G$ ERC-20 token on Celo.
     * @param _celoToken Address of the CELO ERC-20 token on Celo
     *                   (canonical: 0x471EcE3750Da237f93B8E339c536989b8978a438).
     * @param _cusd      Address of the cUSD ERC-20 token on Celo
     *                   (canonical: 0x765DE816845861e75A25fCA122bb6898B8B1282a).
     * @param _usdt      Address of the USDT ERC-20 token on Celo
     *                   (canonical: 0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e).
     */
    constructor(
        address _gd,
        address _celoToken,
        address _cusd,
        address _usdt
    ) {
        require(_gd        != address(0), "Invalid G$ address");
        require(_celoToken != address(0), "Invalid CELO address");
        require(_cusd      != address(0), "Invalid cUSD address");
        require(_usdt      != address(0), "Invalid USDT address");
        require(
            _gd != _celoToken &&
            _gd != _cusd &&
            _gd != _usdt &&
            _celoToken != _cusd &&
            _celoToken != _usdt &&
            _cusd != _usdt,
            "Token addresses must be unique"
        );

        gd        = _gd;
        celoToken = _celoToken;
        cusd      = _cusd;
        usdt      = _usdt;
    }

    // ── Internal helpers ────────────────────────────────────────────────────

    function _isAllowedToken(address token) internal view returns (bool) {
        return token == gd || token == celoToken || token == cusd || token == usdt;
    }

    function _isValidDuration(uint256 days_) internal pure returns (bool) {
        return days_ >= MIN_LOCK_DAYS && days_ <= MAX_LOCK_DAYS;
    }

    function _minMaxFor(address token) internal view returns (uint256 minA, uint256 maxA) {
        if (token == gd) {
            return (MIN_DEPOSIT_GD, MAX_DEPOSIT_GD);
        }
        if (token == celoToken) {
            return (MIN_DEPOSIT_CELO, MAX_DEPOSIT_CELO);
        }
        if (token == cusd) {
            return (MIN_DEPOSIT_CUSD, MAX_DEPOSIT_CUSD);
        }
        return (MIN_DEPOSIT_USDT, MAX_DEPOSIT_USDT);
    }

    function _midTierMinFor(address token) internal view returns (uint256) {
        if (token == gd)             return MID_TIER_MIN_GD;
        if (token == celoToken)      return MID_TIER_MIN_CELO;
        if (token == cusd)           return MID_TIER_MIN_CUSD;
        return MID_TIER_MIN_USDT;
    }

    function _longTierMinFor(address token) internal view returns (uint256) {
        if (token == gd)             return LONG_TIER_MIN_GD;
        if (token == celoToken)      return LONG_TIER_MIN_CELO;
        if (token == cusd)           return LONG_TIER_MIN_CUSD;
        return LONG_TIER_MIN_USDT;
    }

    function _bonusForSlot(address token, uint256 amount, uint256 lockDays) internal view returns (uint256) {
        if (!_isAllowedToken(token)) return 0;
        if (!_isValidDuration(lockDays)) return 0;

        (uint256 minA, ) = _minMaxFor(token);
        if (amount < minA) return 0;

        // 1..29-day "tester" tier: any deposit >= per-token MIN earns 30 G$.
        if (lockDays < 30) {
            return BONUS_TESTER;
        }

        // 30..360-day mid-tier: requires per-token "100k G$ equivalent".
        if (amount < _midTierMinFor(token)) {
            return 0;
        }

        // >=300-day loyalty tier: when amount also clears per-token "1M G$
        // equivalent", REPLACE the mid-tier value with the flat 20,000 G$
        // loyalty bonus.
        if (lockDays >= LOYALTY_MIN_DAYS && amount >= _longTierMinFor(token)) {
            return BONUS_LOYALTY;
        }

        // Mid-tier: linear (lockDays * 500 / 30) G$. Integer division rounds
        // down per day, so e.g. lockDays = 31 → 516 G$; lockDays = 30 → 500;
        // lockDays = 360 → 6,000.
        return (lockDays * BONUS_PER_30D_STEP) / 30;
    }

    function _trackSlotRef(address user, address token, uint256 lockDays) internal {
        if (!_userSlotKnown[user][token][lockDays]) {
            _userSlotRefs[user].push(SlotRef({token: token, lockDays: lockDays}));
            _userSlotKnown[user][token][lockDays] = true;
        }
    }

    // ── User: deposit (open slot or top-up) ─────────────────────────────────

    /**
     * @notice Deposit into a savings slot. Top-ups into an existing
     *         (msg.sender, token, lockDays) slot inherit the original
     *         unlocksAt — the lock period is NOT extended.
     * @param token    Must be one of: gd, celoToken, cusd, usdt.
     * @param amount   Per-deposit amount (must satisfy per-token MIN/MAX).
     *                 The cumulative slot total must also stay within MAX.
     * @param lockDays Any integer in [1, 360].
     */
    function depositSavings(address token, uint256 amount, uint256 lockDays) external nonReentrant {
        require(_isAllowedToken(token), "Token not allowed");
        require(_isValidDuration(lockDays), "Invalid lock duration");

        (uint256 minA, uint256 maxA) = _minMaxFor(token);
        require(amount >= minA, "Below minimum deposit");
        require(amount <= maxA, "Above maximum deposit");

        // Pull tokens
        IERC20(token).safeTransferFrom(msg.sender, address(this), amount);

        DepositSlot storage slot = slots[msg.sender][token][lockDays];
        bool isTopUp = slot.amount > 0;

        if (!isTopUp) {
            slot.firstDepositAt = block.timestamp;
            slot.unlocksAt      = block.timestamp + (lockDays * 1 days);
            slot.bonusClaimed   = false;
            totalSlotsOpened   += 1;
        }

        uint256 newTotal = slot.amount + amount;
        require(newTotal <= maxA, "Slot total above maximum");
        slot.amount = newTotal;

        _trackSlotRef(msg.sender, token, lockDays);

        emit Saved(msg.sender, token, lockDays, amount, newTotal, slot.unlocksAt, isTopUp);
    }

    // ── User: withdraw (matured only) ───────────────────────────────────────

    /**
     * @notice Withdraw a fully-matured slot. Pays principal in `token` plus
     *         G$ bonus (if eligible and the reward pool has enough G$).
     *         Slot is reset to allow a fresh deposit afterwards.
     */
    function withdraw(address token, uint256 lockDays) external nonReentrant {
        require(_isAllowedToken(token), "Token not allowed");

        DepositSlot storage slot = slots[msg.sender][token][lockDays];
        require(slot.amount > 0, "Nothing to withdraw");
        require(block.timestamp >= slot.unlocksAt, "Still locked");

        uint256 principal = slot.amount;
        uint256 bonusG = 0;

        if (!slot.bonusClaimed) {
            uint256 b = _bonusForSlot(token, principal, lockDays);
            if (b > 0 && rewardPool >= b) {
                slot.bonusClaimed = true;
                rewardPool -= b;
                bonusG = b;
            }
        }

        // Reset slot so the user can deposit again in this (token, lockDays)
        slot.amount = 0;
        slot.firstDepositAt = 0;
        slot.unlocksAt = 0;
        slot.bonusClaimed = false;

        // Pay principal in deposit token
        IERC20(token).safeTransfer(msg.sender, principal);

        // Pay bonus in G$ (if eligible)
        if (bonusG > 0) {
            IERC20(gd).safeTransfer(msg.sender, bonusG);
            emit BonusPaid(msg.sender, token, lockDays, bonusG, block.timestamp);
        }

        emit Withdrawn(msg.sender, token, lockDays, principal, block.timestamp);
    }

    // ── Sponsor: fund G$ reward pool ────────────────────────────────────────

    /**
     * @notice Add G$ to the bonus reward pool. Anyone can call.
     * @dev    Funds added here can ONLY be paid out as bonuses to
     *         qualifying savers — they can NEVER be withdrawn by any
     *         account, including the funder.
     */
    function fundRewardPool(uint256 amount) external nonReentrant {
        require(amount > 0, "Amount must be > 0");
        IERC20(gd).safeTransferFrom(msg.sender, address(this), amount);
        rewardPool += amount;
        emit RewardPoolFunded(msg.sender, amount, block.timestamp);
    }

    // ── View functions ──────────────────────────────────────────────────────

    function isAllowedToken(address token) external view returns (bool) {
        return _isAllowedToken(token);
    }

    /**
     * @notice Inclusive day range (min, max) the contract accepts as lock
     *         duration. Any integer in [min, max] is valid.
     */
    function getDurationRange() external pure returns (uint256 minDays, uint256 maxDays) {
        return (MIN_LOCK_DAYS, MAX_LOCK_DAYS);
    }

    function getMinMax(address token) external view returns (uint256 minA, uint256 maxA) {
        require(_isAllowedToken(token), "Token not allowed");
        return _minMaxFor(token);
    }

    /**
     * @notice Returns the bonus a slot of (token, amount, lockDays) qualifies
     *         for. Always denominated in G$.
     */
    function getBonusAmount(address token, uint256 amount, uint256 lockDays) external view returns (uint256) {
        if (!_isAllowedToken(token)) return 0;
        return _bonusForSlot(token, amount, lockDays);
    }

    /**
     * @notice Full details of a single slot.
     */
    function getSlot(address user, address token, uint256 lockDays) external view returns (
        uint256 amount,
        uint256 firstDepositAt,
        uint256 unlocksAt,
        bool    bonusClaimed,
        bool    isUnlocked,
        uint256 pendingBonus
    ) {
        DepositSlot storage s = slots[user][token][lockDays];
        uint256 b = (s.amount > 0 && !s.bonusClaimed) ? _bonusForSlot(token, s.amount, lockDays) : 0;
        return (
            s.amount,
            s.firstDepositAt,
            s.unlocksAt,
            s.bonusClaimed,
            s.amount > 0 && block.timestamp >= s.unlocksAt,
            b
        );
    }

    /**
     * @notice All (token, lockDays) the user has ever opened, including
     *         currently-empty slots. For active-only enumeration use
     *         getUserActiveSlots().
     */
    function getUserSlotRefs(address user) external view returns (SlotRef[] memory) {
        return _userSlotRefs[user];
    }

    /**
     * @notice Returns parallel arrays describing all currently-active slots
     *         for `user` (those with amount > 0).
     */
    function getUserActiveSlots(address user) external view returns (
        address[] memory tokens,
        uint256[] memory lockDays_,
        uint256[] memory amounts,
        uint256[] memory unlocksAts,
        bool[]    memory areUnlocked,
        bool[]    memory bonusClaimed,
        uint256[] memory pendingBonuses
    ) {
        SlotRef[] storage all = _userSlotRefs[user];
        uint256 n = all.length;
        uint256 active = 0;
        for (uint256 i = 0; i < n; i++) {
            if (slots[user][all[i].token][all[i].lockDays].amount > 0) active++;
        }

        tokens         = new address[](active);
        lockDays_      = new uint256[](active);
        amounts        = new uint256[](active);
        unlocksAts     = new uint256[](active);
        areUnlocked    = new bool[](active);
        bonusClaimed   = new bool[](active);
        pendingBonuses = new uint256[](active);

        uint256 j = 0;
        for (uint256 i = 0; i < n; i++) {
            DepositSlot storage s = slots[user][all[i].token][all[i].lockDays];
            if (s.amount == 0) continue;
            tokens[j]         = all[i].token;
            lockDays_[j]      = all[i].lockDays;
            amounts[j]        = s.amount;
            unlocksAts[j]     = s.unlocksAt;
            areUnlocked[j]    = block.timestamp >= s.unlocksAt;
            bonusClaimed[j]   = s.bonusClaimed;
            pendingBonuses[j] = s.bonusClaimed ? 0 : _bonusForSlot(all[i].token, s.amount, all[i].lockDays);
            j++;
        }
    }

    /**
     * @notice Aggregate stats for the whole contract.
     * @return totalLockedGd      Sum of G$ locked in user slots
     *                            (= contract G$ balance minus rewardPool).
     * @return totalLockedCelo    Total CELO held by the contract.
     * @return totalLockedCusd    Total cUSD held by the contract.
     * @return totalLockedUsdt    Total USDT held by the contract.
     * @return rewardPoolBalance  Current G$ reward pool.
     * @return contractGdBalance  Raw contract G$ balance.
     * @return contractCeloBalance Raw contract CELO balance.
     * @return contractCusdBalance Raw contract cUSD balance.
     * @return contractUsdtBalance Raw contract USDT balance.
     * @return slotsOpenedTotal   Cumulative count of slot openings.
     */
    function getContractStats() external view returns (
        uint256 totalLockedGd,
        uint256 totalLockedCelo,
        uint256 totalLockedCusd,
        uint256 totalLockedUsdt,
        uint256 rewardPoolBalance,
        uint256 contractGdBalance,
        uint256 contractCeloBalance,
        uint256 contractCusdBalance,
        uint256 contractUsdtBalance,
        uint256 slotsOpenedTotal
    ) {
        uint256 balGd   = IERC20(gd).balanceOf(address(this));
        uint256 balCelo = IERC20(celoToken).balanceOf(address(this));
        uint256 balCusd = IERC20(cusd).balanceOf(address(this));
        uint256 balUsdt = IERC20(usdt).balanceOf(address(this));
        return (
            balGd > rewardPool ? balGd - rewardPool : 0,
            balCelo,
            balCusd,
            balUsdt,
            rewardPool,
            balGd,
            balCelo,
            balCusd,
            balUsdt,
            totalSlotsOpened
        );
    }

    function getTokens() external view returns (
        address gdAddr,
        address celoAddr,
        address cusdAddr,
        address usdtAddr
    ) {
        return (gd, celoToken, cusd, usdt);
    }
}
