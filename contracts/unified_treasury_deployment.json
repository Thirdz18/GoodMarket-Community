// SPDX-License-Identifier: MIT
pragma solidity ^0.8.21;

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

library Address {
    function isContract(address account) internal view returns (bool) {
        return account.code.length > 0;
    }
}

library SafeERC20 {
    using Address for address;

    function safeTransfer(IERC20 token, address to, uint256 value) internal {
        (bool success, bytes memory data) = address(token).call(
            abi.encodeWithSelector(token.transfer.selector, to, value)
        );
        require(success && (data.length == 0 || abi.decode(data, (bool))), "SafeERC20: transfer failed");
    }

    function safeTransferFrom(IERC20 token, address from, address to, uint256 value) internal {
        (bool success, bytes memory data) = address(token).call(
            abi.encodeWithSelector(token.transferFrom.selector, from, to, value)
        );
        require(success && (data.length == 0 || abi.decode(data, (bool))), "SafeERC20: transferFrom failed");
    }
}

/**
 * @title UnifiedTreasury
 * @notice Central treasury for GoodMarket platform.
 *         Anyone can deposit G$ tokens.
 *         Only the authorized signer (LEARN_WALLET_PRIVATE_KEY) can distribute
 *         funds to the hardcoded recipient addresses.
 */
contract UnifiedTreasury {
    using SafeERC20 for IERC20;

    IERC20 public immutable goodDollarToken;
    address public immutable authorizedSigner;

    // Hardcoded recipient addresses — set once at deployment, never changeable
    address public immutable learnEarnContract;
    address public immutable dailyTaskContract;
    address public immutable discourseWallet;
    address public immutable minigamesWallet;
    address public immutable communityStoriesWallet;
    address public immutable referralWallet;

    // Recipient labels for event tracking
    string public constant LABEL_LEARN_EARN       = "learn_earn";
    string public constant LABEL_DAILY_TASK        = "daily_task";
    string public constant LABEL_DISCOURSE         = "discourse";
    string public constant LABEL_MINIGAMES         = "minigames";
    string public constant LABEL_COMMUNITY_STORIES = "community_stories";
    string public constant LABEL_REFERRAL          = "referral";

    // Totals
    uint256 public totalDeposited;
    uint256 public totalDistributed;

    // Events
    event Deposited(address indexed from, uint256 amount, uint256 timestamp);
    event Distributed(address indexed to, string label, uint256 amount, uint256 timestamp);
    event EmergencyWithdraw(address indexed to, uint256 amount, uint256 timestamp);

    modifier onlyAuthorized() {
        require(msg.sender == authorizedSigner, "UnifiedTreasury: caller is not authorized signer");
        _;
    }

    /**
     * @param _goodDollarToken   Address of the G$ ERC-20 token
     * @param _authorizedSigner  Wallet that can call distribute() — LEARN_WALLET_PRIVATE_KEY address
     * @param _learnEarnContract Learn & Earn contract address
     * @param _dailyTaskContract Daily Task contract address
     * @param _discourseWallet   Discourse task wallet address
     * @param _minigamesWallet   Minigames wallet address
     * @param _communityWallet   Community Stories wallet address
     * @param _referralWallet    Referral program wallet address
     */
    constructor(
        address _goodDollarToken,
        address _authorizedSigner,
        address _learnEarnContract,
        address _dailyTaskContract,
        address _discourseWallet,
        address _minigamesWallet,
        address _communityWallet,
        address _referralWallet
    ) {
        require(_goodDollarToken    != address(0), "Invalid GD token");
        require(_authorizedSigner   != address(0), "Invalid signer");
        require(_learnEarnContract  != address(0), "Invalid learnEarn");
        require(_dailyTaskContract  != address(0), "Invalid dailyTask");
        require(_discourseWallet    != address(0), "Invalid discourse");
        require(_minigamesWallet    != address(0), "Invalid minigames");
        require(_communityWallet    != address(0), "Invalid community");
        require(_referralWallet     != address(0), "Invalid referral");

        goodDollarToken       = IERC20(_goodDollarToken);
        authorizedSigner      = _authorizedSigner;
        learnEarnContract     = _learnEarnContract;
        dailyTaskContract     = _dailyTaskContract;
        discourseWallet       = _discourseWallet;
        minigamesWallet       = _minigamesWallet;
        communityStoriesWallet = _communityWallet;
        referralWallet        = _referralWallet;
    }

    /**
     * @notice Anyone can deposit G$ tokens into this treasury.
     *         Caller must have approved this contract to spend `amount` G$ first.
     * @param amount Amount in G$ base units (2 decimals: 1 G$ = 100)
     */
    function deposit(uint256 amount) external {
        require(amount > 0, "Amount must be > 0");
        goodDollarToken.safeTransferFrom(msg.sender, address(this), amount);
        totalDeposited += amount;
        emit Deposited(msg.sender, amount, block.timestamp);
    }

    /**
     * @notice Distribute G$ from this treasury to a hardcoded recipient.
     *         Only the authorized signer can call this.
     * @param recipientKey  One of: "learn_earn", "daily_task", "discourse",
     *                      "minigames", "community_stories", "referral"
     * @param amount        Amount in G$ base units
     */
    function distribute(string calldata recipientKey, uint256 amount) external onlyAuthorized {
        require(amount > 0, "Amount must be > 0");
        require(
            getContractBalance() >= amount,
            "Insufficient treasury balance"
        );

        address recipient = _resolveRecipient(recipientKey);
        require(recipient != address(0), "Unknown recipient key");

        goodDollarToken.safeTransfer(recipient, amount);
        totalDistributed += amount;
        emit Distributed(recipient, recipientKey, amount, block.timestamp);
    }

    /**
     * @notice Emergency withdraw all G$ to the authorized signer.
     *         Only callable by the authorized signer.
     */
    function emergencyWithdraw() external onlyAuthorized {
        uint256 balance = getContractBalance();
        require(balance > 0, "Nothing to withdraw");
        goodDollarToken.safeTransfer(authorizedSigner, balance);
        emit EmergencyWithdraw(authorizedSigner, balance, block.timestamp);
    }

    // ─── View Functions ───────────────────────────────────────────────────────

    function getContractBalance() public view returns (uint256) {
        return goodDollarToken.balanceOf(address(this));
    }

    function getRecipientAddress(string calldata recipientKey) external view returns (address) {
        return _resolveRecipient(recipientKey);
    }

    function getAllRecipients() external view returns (
        address learnEarn,
        address dailyTask,
        address discourse,
        address minigames,
        address communityStories,
        address referral
    ) {
        return (
            learnEarnContract,
            dailyTaskContract,
            discourseWallet,
            minigamesWallet,
            communityStoriesWallet,
            referralWallet
        );
    }

    function getStats() external view returns (
        uint256 balance,
        uint256 deposited,
        uint256 distributed
    ) {
        return (getContractBalance(), totalDeposited, totalDistributed);
    }

    // ─── Internal ─────────────────────────────────────────────────────────────

    function _resolveRecipient(string calldata key) internal view returns (address) {
        bytes32 k = keccak256(abi.encodePacked(key));
        if (k == keccak256(abi.encodePacked(LABEL_LEARN_EARN)))       return learnEarnContract;
        if (k == keccak256(abi.encodePacked(LABEL_DAILY_TASK)))        return dailyTaskContract;
        if (k == keccak256(abi.encodePacked(LABEL_DISCOURSE)))         return discourseWallet;
        if (k == keccak256(abi.encodePacked(LABEL_MINIGAMES)))         return minigamesWallet;
        if (k == keccak256(abi.encodePacked(LABEL_COMMUNITY_STORIES))) return communityStoriesWallet;
        if (k == keccak256(abi.encodePacked(LABEL_REFERRAL)))          return referralWallet;
        return address(0);
    }
}
