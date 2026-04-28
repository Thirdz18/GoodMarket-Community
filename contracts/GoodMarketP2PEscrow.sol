// SPDX-License-Identifier: MIT
pragma solidity ^0.8.21;

/**
 * @title GoodMarketP2PEscrow
 * @notice Trustless P2P escrow for trading G$ ↔ fiat off-platform.
 *
 * Architecture: Hybrid pre-funded escrow.
 *   - Seller "opens" an ad by depositing the full G$ amount into the contract.
 *   - Buyers "place orders" against the ad, locking a portion until payment is settled.
 *   - Seller "closes" the ad anytime there are no active trades, withdrawing remainder.
 *
 * Trust model: Fully trustless for users.
 *   - msg.sender is the authenticated party (no operator-relayed calls).
 *   - Contract holds funds; admin cannot withdraw on behalf of users.
 *   - Admin role is limited to dispute resolution and emergency pause.
 *
 * Lifecycle:
 *   AD:    Open → Closed | Exhausted | Suspended
 *   TRADE: PaymentPending → AwaitingRelease → Completed | Disputed → Completed | Refunded
 *                       └→ Cancelled | Expired (refunds back to ad)
 *
 * Off-chain responsibilities (kept in DB / app):
 *   - Fiat currency, payment method, payment instructions
 *   - Payment proofs (screenshots), buyer/seller chat, ratings, dispute reasons
 *   - Dispute investigation (admin reviews proofs, then calls resolveDispute)
 */

interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function transfer(address to, uint256 amount) external returns (bool);
    function balanceOf(address owner) external view returns (uint256);
}

contract GoodMarketP2PEscrow {
    // ─── Roles & Admin State ────────────────────────────────────────────────

    address public owner;        // contract deployer; can transfer, pause, change arbiter
    address public arbiter;      // resolves disputes
    bool    public paused;       // emergency stop

    IERC20  public immutable gDollar;  // G$ token contract address (immutable for safety)

    // ─── Configuration Constants ────────────────────────────────────────────

    uint256 public constant MIN_AD_AMOUNT      = 20_000 ether;  // 20,000 G$ (18 decimals)
    uint256 public constant MIN_PAYMENT_WINDOW = 15 minutes;
    uint256 public constant MAX_PAYMENT_WINDOW = 6 hours;
    uint256 public constant AUTO_RELEASE_DELAY = 48 hours;      // after markPaid, before auto-release allowed

    // ─── Data Structures ────────────────────────────────────────────────────

    struct Ad {
        address seller;
        uint256 totalLocked;       // G$ initially deposited
        uint256 remainingAmount;   // G$ available for new orders
        uint256 minOrder;          // min G$ per order
        uint256 maxOrder;          // max G$ per order
        uint32  activeTradeCount;  // count of unresolved trades against this ad
        bool    open;              // true if ad is still accepting orders
        bool    exists;            // sentinel
    }

    enum TradeStatus {
        None,             // 0 — not created (sentinel)
        PaymentPending,   // 1 — buyer placed order, waiting for fiat
        AwaitingRelease,  // 2 — buyer marked paid, waiting for seller approval
        Completed,        // 3 — seller approved, G$ released to buyer (final)
        Cancelled,        // 4 — cancelled before payment, G$ returned to ad (final)
        Expired,          // 5 — deadline passed, G$ returned to ad (final)
        Disputed,         // 6 — opened by buyer or seller, awaiting arbiter
        Refunded          // 7 — arbiter ruled for seller, G$ returned to seller (final)
    }

    struct Trade {
        bytes32 adId;
        address buyer;
        uint256 amount;
        uint64  deadline;        // payment-pending expires at this timestamp
        uint64  markedPaidAt;    // 0 unless buyer marked paid
        TradeStatus status;
        bool    exists;
    }

    mapping(bytes32 => Ad) public ads;
    mapping(bytes32 => Trade) public trades;

    // ─── Events ─────────────────────────────────────────────────────────────

    event AdOpened(
        bytes32 indexed adId,
        address indexed seller,
        uint256 totalLocked,
        uint256 minOrder,
        uint256 maxOrder
    );
    event AdClosed(bytes32 indexed adId, address indexed seller, uint256 refundedAmount);
    event AdExhausted(bytes32 indexed adId);

    event OrderPlaced(
        bytes32 indexed tradeId,
        bytes32 indexed adId,
        address indexed buyer,
        uint256 amount,
        uint64 deadline
    );
    event OrderCancelled(bytes32 indexed tradeId, address by);
    event OrderExpired(bytes32 indexed tradeId);
    event MarkedPaid(bytes32 indexed tradeId);
    event Released(bytes32 indexed tradeId, address indexed buyer, uint256 amount);
    event AutoReleased(bytes32 indexed tradeId);
    event Disputed(bytes32 indexed tradeId, address by);
    event Resolved(bytes32 indexed tradeId, bool buyerWins, address indexed winner);

    event Paused(address by);
    event Unpaused(address by);
    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);
    event ArbiterChanged(address indexed previousArbiter, address indexed newArbiter);

    // ─── Modifiers ──────────────────────────────────────────────────────────

    modifier onlyOwner() {
        require(msg.sender == owner, "P2P: caller is not owner");
        _;
    }

    modifier onlyArbiter() {
        require(msg.sender == arbiter, "P2P: caller is not arbiter");
        _;
    }

    modifier whenNotPaused() {
        require(!paused, "P2P: contract is paused");
        _;
    }

    // ─── Reentrancy Guard ───────────────────────────────────────────────────

    uint256 private _reentrancyStatus = 1;
    modifier nonReentrant() {
        require(_reentrancyStatus == 1, "P2P: reentrant call");
        _reentrancyStatus = 2;
        _;
        _reentrancyStatus = 1;
    }

    // ─── Constructor ────────────────────────────────────────────────────────

    /**
     * @param _gDollar  Address of the G$ ERC-20 token on Celo
     * @param _arbiter  Initial arbiter wallet (typically same as owner for v1)
     */
    constructor(address _gDollar, address _arbiter) {
        require(_gDollar != address(0), "P2P: gDollar cannot be zero");
        require(_arbiter != address(0), "P2P: arbiter cannot be zero");
        owner   = msg.sender;
        arbiter = _arbiter;
        gDollar = IERC20(_gDollar);
        emit OwnershipTransferred(address(0), msg.sender);
        emit ArbiterChanged(address(0), _arbiter);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // SELLER ACTIONS
    // ═══════════════════════════════════════════════════════════════════════

    /**
     * @notice Seller opens an ad by depositing G$ into escrow.
     * @dev Requires prior `gDollar.approve(this, totalAmount)`.
     *      adId must be unique and may be generated client-side
     *      (e.g. keccak256(seller, nonce)).
     */
    function openAd(
        bytes32 adId,
        uint256 totalAmount,
        uint256 minOrder,
        uint256 maxOrder
    ) external whenNotPaused nonReentrant {
        require(!ads[adId].exists, "P2P: ad already exists");
        require(totalAmount >= MIN_AD_AMOUNT, "P2P: below MIN_AD_AMOUNT");
        require(minOrder >= MIN_AD_AMOUNT,    "P2P: minOrder below MIN_AD_AMOUNT");
        require(maxOrder >= minOrder,         "P2P: maxOrder < minOrder");
        require(maxOrder <= totalAmount,      "P2P: maxOrder > totalAmount");

        // Pull G$ from seller
        require(
            gDollar.transferFrom(msg.sender, address(this), totalAmount),
            "P2P: G$ transferFrom failed"
        );

        ads[adId] = Ad({
            seller: msg.sender,
            totalLocked: totalAmount,
            remainingAmount: totalAmount,
            minOrder: minOrder,
            maxOrder: maxOrder,
            activeTradeCount: 0,
            open: true,
            exists: true
        });

        emit AdOpened(adId, msg.sender, totalAmount, minOrder, maxOrder);
    }

    /**
     * @notice Seller cancels their own ad. Only allowed if no active trades.
     * @dev Refunds the remainingAmount back to the seller's wallet.
     *      Also callable as a "withdraw dust" once an ad is exhausted.
     */
    function closeAd(bytes32 adId) external nonReentrant {
        Ad storage ad = ads[adId];
        require(ad.exists, "P2P: ad not found");
        require(ad.seller == msg.sender, "P2P: not your ad");
        require(ad.open, "P2P: ad already closed");
        require(ad.activeTradeCount == 0, "P2P: ad has active trades");

        uint256 refund = ad.remainingAmount;
        ad.remainingAmount = 0;
        ad.open = false;

        if (refund > 0) {
            require(gDollar.transfer(msg.sender, refund), "P2P: G$ transfer failed");
        }

        emit AdClosed(adId, msg.sender, refund);
    }

    /**
     * @notice Seller approves a trade and releases G$ to buyer.
     * @dev Only callable when trade is in AwaitingRelease (buyer marked paid).
     */
    function release(bytes32 tradeId) external nonReentrant {
        Trade storage t = trades[tradeId];
        require(t.exists, "P2P: trade not found");
        require(t.status == TradeStatus.AwaitingRelease, "P2P: trade not awaiting release");

        Ad storage ad = ads[t.adId];
        require(ad.seller == msg.sender, "P2P: not the seller");

        t.status = TradeStatus.Completed;
        if (ad.activeTradeCount > 0) {
            ad.activeTradeCount -= 1;
        }

        require(gDollar.transfer(t.buyer, t.amount), "P2P: G$ transfer failed");

        emit Released(tradeId, t.buyer, t.amount);

        _checkAdExhausted(t.adId);
    }

    /**
     * @notice Seller opens a dispute (e.g., did not receive fiat payment).
     * @dev Only callable while trade is AwaitingRelease. Funds locked until arbiter resolves.
     */
    function disputeAsSeller(bytes32 tradeId) external {
        Trade storage t = trades[tradeId];
        require(t.exists, "P2P: trade not found");
        require(t.status == TradeStatus.AwaitingRelease, "P2P: cannot dispute now");

        Ad storage ad = ads[t.adId];
        require(ad.seller == msg.sender, "P2P: not the seller");

        t.status = TradeStatus.Disputed;
        emit Disputed(tradeId, msg.sender);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // BUYER ACTIONS
    // ═══════════════════════════════════════════════════════════════════════

    /**
     * @notice Buyer places an order against an open ad.
     * @dev Locks `amount` from the ad's remainingAmount.
     *      tradeId must be unique; deadline is the timestamp by which buyer must mark paid.
     */
    function placeOrder(
        bytes32 adId,
        bytes32 tradeId,
        uint256 amount,
        uint64  deadline
    ) external whenNotPaused nonReentrant {
        Ad storage ad = ads[adId];
        require(ad.exists,                "P2P: ad not found");
        require(ad.open,                  "P2P: ad not open");
        require(!trades[tradeId].exists,  "P2P: trade already exists");
        require(msg.sender != ad.seller,  "P2P: cannot trade with self");
        require(amount >= ad.minOrder,    "P2P: amount below minOrder");
        require(amount <= ad.maxOrder,    "P2P: amount above maxOrder");
        require(amount <= ad.remainingAmount, "P2P: insufficient ad remaining");

        require(deadline > block.timestamp,        "P2P: deadline in past");
        uint256 window = uint256(deadline) - block.timestamp;
        require(window >= MIN_PAYMENT_WINDOW,      "P2P: window too short");
        require(window <= MAX_PAYMENT_WINDOW,      "P2P: window too long");

        ad.remainingAmount -= amount;
        ad.activeTradeCount += 1;

        trades[tradeId] = Trade({
            adId: adId,
            buyer: msg.sender,
            amount: amount,
            deadline: deadline,
            markedPaidAt: 0,
            status: TradeStatus.PaymentPending,
            exists: true
        });

        emit OrderPlaced(tradeId, adId, msg.sender, amount, deadline);
    }

    /**
     * @notice Buyer cancels their order BEFORE marking paid.
     * @dev Returns the locked amount to the ad's remainingAmount.
     *      Not allowed once status is AwaitingRelease (buyer already claimed payment).
     */
    function cancelOrder(bytes32 tradeId) external nonReentrant {
        Trade storage t = trades[tradeId];
        require(t.exists,                                 "P2P: trade not found");
        require(t.status == TradeStatus.PaymentPending,   "P2P: cannot cancel now");
        require(t.buyer == msg.sender,                    "P2P: not the buyer");

        t.status = TradeStatus.Cancelled;
        _returnAmountToAd(t.adId, t.amount);

        emit OrderCancelled(tradeId, msg.sender);
    }

    /**
     * @notice Buyer signals fiat payment has been sent.
     * @dev Off-chain, buyer must also upload proof (handled by app/DB).
     *      Transitions trade to AwaitingRelease and starts auto-release timer.
     */
    function markPaid(bytes32 tradeId) external {
        Trade storage t = trades[tradeId];
        require(t.exists,                                 "P2P: trade not found");
        require(t.status == TradeStatus.PaymentPending,   "P2P: not in PaymentPending");
        require(t.buyer == msg.sender,                    "P2P: not the buyer");
        require(block.timestamp <= t.deadline,            "P2P: deadline passed");

        t.status = TradeStatus.AwaitingRelease;
        t.markedPaidAt = uint64(block.timestamp);
        emit MarkedPaid(tradeId);
    }

    /**
     * @notice Buyer opens a dispute (e.g., seller is unresponsive after marking paid).
     * @dev Only callable while trade is AwaitingRelease. Funds locked until arbiter resolves.
     */
    function disputeAsBuyer(bytes32 tradeId) external {
        Trade storage t = trades[tradeId];
        require(t.exists, "P2P: trade not found");
        require(t.status == TradeStatus.AwaitingRelease, "P2P: cannot dispute now");
        require(t.buyer == msg.sender, "P2P: not the buyer");

        t.status = TradeStatus.Disputed;
        emit Disputed(tradeId, msg.sender);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // PERMISSIONLESS / KEEPER ACTIONS
    // ═══════════════════════════════════════════════════════════════════════

    /**
     * @notice Anyone may call this to expire a PaymentPending order whose deadline has passed.
     *         Refunds the locked G$ back to the ad. Used by a keeper / cron job.
     */
    function expirePendingOrder(bytes32 tradeId) external nonReentrant {
        Trade storage t = trades[tradeId];
        require(t.exists,                                 "P2P: trade not found");
        require(t.status == TradeStatus.PaymentPending,   "P2P: not pending");
        require(block.timestamp > t.deadline,             "P2P: deadline not reached");

        t.status = TradeStatus.Expired;
        _returnAmountToAd(t.adId, t.amount);

        emit OrderExpired(tradeId);
    }

    /**
     * @notice Anyone may call this to auto-release a trade if seller has not acted within
     *         AUTO_RELEASE_DELAY after buyer marked paid. Sends G$ to buyer.
     * @dev Cannot be called if trade is Disputed.
     */
    function autoReleaseAfterTimeout(bytes32 tradeId) external nonReentrant {
        Trade storage t = trades[tradeId];
        require(t.exists,                                  "P2P: trade not found");
        require(t.status == TradeStatus.AwaitingRelease,   "P2P: not awaiting release");
        require(t.markedPaidAt > 0,                        "P2P: never marked paid");
        require(
            block.timestamp >= uint256(t.markedPaidAt) + AUTO_RELEASE_DELAY,
            "P2P: auto-release not yet"
        );

        t.status = TradeStatus.Completed;

        Ad storage ad = ads[t.adId];
        if (ad.activeTradeCount > 0) {
            ad.activeTradeCount -= 1;
        }

        require(gDollar.transfer(t.buyer, t.amount), "P2P: G$ transfer failed");

        emit AutoReleased(tradeId);
        emit Released(tradeId, t.buyer, t.amount);

        _checkAdExhausted(t.adId);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // ARBITER ACTIONS
    // ═══════════════════════════════════════════════════════════════════════

    /**
     * @notice Arbiter resolves a disputed trade.
     * @param tradeId   The trade to resolve.
     * @param buyerWins true → release G$ to buyer (Completed)
     *                  false → refund G$ to seller's wallet (Refunded)
     *                  Refund goes directly to seller, NOT back to ad.remainingAmount,
     *                  to avoid an attacker disputing then re-orchestrating cancellation.
     */
    function resolveDispute(bytes32 tradeId, bool buyerWins)
        external
        onlyArbiter
        nonReentrant
    {
        Trade storage t = trades[tradeId];
        require(t.exists, "P2P: trade not found");
        require(t.status == TradeStatus.Disputed, "P2P: not disputed");

        Ad storage ad = ads[t.adId];
        if (ad.activeTradeCount > 0) {
            ad.activeTradeCount -= 1;
        }

        address winner;
        if (buyerWins) {
            t.status = TradeStatus.Completed;
            winner = t.buyer;
            require(gDollar.transfer(t.buyer, t.amount), "P2P: G$ transfer failed");
            emit Released(tradeId, t.buyer, t.amount);
        } else {
            t.status = TradeStatus.Refunded;
            winner = ad.seller;
            require(gDollar.transfer(ad.seller, t.amount), "P2P: G$ transfer failed");
        }

        emit Resolved(tradeId, buyerWins, winner);

        _checkAdExhausted(t.adId);
    }

    // ═══════════════════════════════════════════════════════════════════════
    // OWNER / ADMIN ACTIONS
    // ═══════════════════════════════════════════════════════════════════════

    function pause() external onlyOwner {
        require(!paused, "P2P: already paused");
        paused = true;
        emit Paused(msg.sender);
    }

    function unpause() external onlyOwner {
        require(paused, "P2P: not paused");
        paused = false;
        emit Unpaused(msg.sender);
    }

    function transferOwnership(address newOwner) external onlyOwner {
        require(newOwner != address(0), "P2P: zero address");
        emit OwnershipTransferred(owner, newOwner);
        owner = newOwner;
    }

    function setArbiter(address newArbiter) external onlyOwner {
        require(newArbiter != address(0), "P2P: zero address");
        emit ArbiterChanged(arbiter, newArbiter);
        arbiter = newArbiter;
    }

    // ═══════════════════════════════════════════════════════════════════════
    // INTERNAL HELPERS
    // ═══════════════════════════════════════════════════════════════════════

    function _returnAmountToAd(bytes32 adId, uint256 amount) internal {
        Ad storage ad = ads[adId];
        ad.remainingAmount += amount;
        if (ad.activeTradeCount > 0) {
            ad.activeTradeCount -= 1;
        }
    }

    function _checkAdExhausted(bytes32 adId) internal {
        Ad storage ad = ads[adId];
        if (ad.open && ad.remainingAmount < ad.minOrder && ad.activeTradeCount == 0) {
            // Ad is exhausted (or below min) and has no active trades.
            // Mark closed; seller can call closeAd() to retrieve any dust.
            // We don't auto-transfer here so the seller maintains control.
            emit AdExhausted(adId);
        }
    }

    // ═══════════════════════════════════════════════════════════════════════
    // VIEW HELPERS
    // ═══════════════════════════════════════════════════════════════════════

    function getAd(bytes32 adId) external view returns (
        address seller,
        uint256 totalLocked,
        uint256 remainingAmount,
        uint256 minOrder,
        uint256 maxOrder,
        uint32  activeTradeCount,
        bool    open
    ) {
        Ad storage ad = ads[adId];
        require(ad.exists, "P2P: ad not found");
        return (
            ad.seller,
            ad.totalLocked,
            ad.remainingAmount,
            ad.minOrder,
            ad.maxOrder,
            ad.activeTradeCount,
            ad.open
        );
    }

    function getTrade(bytes32 tradeId) external view returns (
        bytes32 adId,
        address buyer,
        uint256 amount,
        uint64  deadline,
        uint64  markedPaidAt,
        TradeStatus status
    ) {
        Trade storage t = trades[tradeId];
        require(t.exists, "P2P: trade not found");
        return (
            t.adId,
            t.buyer,
            t.amount,
            t.deadline,
            t.markedPaidAt,
            t.status
        );
    }

    function totalEscrowed() external view returns (uint256) {
        return gDollar.balanceOf(address(this));
    }
}
