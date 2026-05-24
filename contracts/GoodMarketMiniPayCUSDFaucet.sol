// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IERC20 {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
}

/**
 * @title GoodMarketMiniPayCUSDFaucet
 * @notice Non-custodial-style faucet pool for MiniPay cUSD disbursements.
 *
 * Design constraints requested:
 * - Anyone can deposit cUSD into this contract.
 * - No withdraw/emergency withdraw function.
 * - No admin/owner role and no mutable operator list.
 */
contract GoodMarketMiniPayCUSDFaucet {
    IERC20 public immutable cUSD;
    address public immutable disburser;
    uint256 public immutable cooldownSeconds;
    mapping(address => uint256) public lastDisbursedAt;

    event CUSDDeposited(address indexed depositor, uint256 amount, uint256 timestamp);
    event GoodMarketTopWallet(
        address indexed recipient,
        address indexed operator,
        uint256 amount,
        bytes32 indexed correlationId,
        string sourceTag,
        uint256 timestamp
    );
    event FaucetCooldownRecorded(address indexed recipient, uint256 lastDisbursedAt, uint256 cooldownSeconds);

    modifier onlyDisburser() {
        require(msg.sender == disburser, "not_disburser");
        _;
    }

    constructor(address cUSDToken, address fixedDisburser, uint256 fixedCooldownSeconds) {
        require(cUSDToken != address(0), "zero_cusd");
        require(fixedDisburser != address(0), "zero_disburser");
        require(fixedCooldownSeconds > 0, "zero_cooldown");

        cUSD = IERC20(cUSDToken);
        disburser = fixedDisburser;
        cooldownSeconds = fixedCooldownSeconds;
    }

    /**
     * @notice Deposit cUSD into faucet pool using ERC20 allowance flow.
     * Anyone can call this after approving cUSD for this contract.
     */
    function depositCUSD(uint256 amount) external returns (bool) {
        require(amount > 0, "zero_amount");
        bool ok = cUSD.transferFrom(msg.sender, address(this), amount);
        require(ok, "cusd_transferfrom_failed");
        emit CUSDDeposited(msg.sender, amount, block.timestamp);
        return true;
    }

    function disburseCUSD(
        address recipient,
        uint256 amount,
        bytes32 correlationId,
        string calldata sourceTag
    ) external onlyDisburser returns (bool) {
        require(recipient != address(0), "zero_recipient");
        require(amount > 0, "zero_amount");
        uint256 last = lastDisbursedAt[recipient];
        if (last > 0) {
            require(block.timestamp >= last + cooldownSeconds, "recipient_cooldown_active");
        }

        bool ok = cUSD.transfer(recipient, amount);
        require(ok, "cusd_transfer_failed");
        lastDisbursedAt[recipient] = block.timestamp;
        emit FaucetCooldownRecorded(recipient, block.timestamp, cooldownSeconds);

        emit GoodMarketTopWallet(
            recipient,
            msg.sender,
            amount,
            correlationId,
            sourceTag,
            block.timestamp
        );
        return true;
    }

    function faucetBalance() external view returns (uint256) {
        return cUSD.balanceOf(address(this));
    }

    function cooldownRemaining(address recipient) external view returns (uint256) {
        uint256 last = lastDisbursedAt[recipient];
        if (last == 0) return 0;
        uint256 unlockAt = last + cooldownSeconds;
        if (block.timestamp >= unlockAt) return 0;
        return unlockAt - block.timestamp;
    }
}
