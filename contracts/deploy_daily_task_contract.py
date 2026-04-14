"""
DailyTaskRewards Contract Deployment Script for Celo Mainnet

This script deploys the DailyTaskRewards contract to Celo mainnet
using TASK_KEY as the contract owner/signer for all disbursements.

Reward: 100 G$ per approved daily task (Twitter or Telegram)
Owner: TASK_KEY wallet address
"""

import os
import json
import logging
from web3 import Web3
from eth_account import Account
from solcx import compile_standard, install_solc

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

CELO_RPC_URL = os.getenv('CELO_RPC_URL', 'https://forno.celo.org')
CHAIN_ID = int(os.getenv('CHAIN_ID', 42220))
GOODDOLLAR_CONTRACT = os.getenv('GOODDOLLAR_CONTRACT_ADDRESS', '')

REWARD_AMOUNT = 100 * 10**18

FLATTENED_SOURCE = """// SPDX-License-Identifier: MIT
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

    function functionCall(address target, bytes memory data) internal returns (bytes memory) {
        return functionCall(target, data, "Address: low-level call failed");
    }

    function functionCall(address target, bytes memory data, string memory errorMessage) internal returns (bytes memory) {
        require(isContract(target), "Address: call to non-contract");
        (bool success, bytes memory returndata) = target.call(data);
        if (success) {
            return returndata;
        } else {
            if (returndata.length > 0) {
                assembly {
                    let returndata_size := mload(returndata)
                    revert(add(32, returndata), returndata_size)
                }
            } else {
                revert(errorMessage);
            }
        }
    }
}

library SafeERC20 {
    using Address for address;

    function safeTransfer(IERC20 token, address to, uint256 value) internal {
        _callOptionalReturn(token, abi.encodeWithSelector(token.transfer.selector, to, value));
    }

    function safeTransferFrom(IERC20 token, address from, address to, uint256 value) internal {
        _callOptionalReturn(token, abi.encodeWithSelector(token.transferFrom.selector, from, to, value));
    }

    function safeApprove(IERC20 token, address spender, uint256 value) internal {
        require((value == 0) || (token.allowance(address(this), spender) == 0), "SafeERC20: approve from non-zero to non-zero allowance");
        _callOptionalReturn(token, abi.encodeWithSelector(token.approve.selector, spender, value));
    }

    function _callOptionalReturn(IERC20 token, bytes memory data) private {
        bytes memory returndata = address(token).functionCall(data, "SafeERC20: low-level call failed");
        if (returndata.length > 0) {
            require(abi.decode(returndata, (bool)), "SafeERC20: ERC20 operation did not succeed");
        }
    }
}

abstract contract Context {
    function _msgSender() internal view virtual returns (address) {
        return msg.sender;
    }
    function _msgData() internal view virtual returns (bytes calldata) {
        return msg.data;
    }
}

abstract contract Ownable is Context {
    address private _owner;

    event OwnershipTransferred(address indexed previousOwner, address indexed newOwner);

    constructor(address initialOwner) {
        _transferOwnership(initialOwner);
    }

    modifier onlyOwner() {
        _checkOwner();
        _;
    }

    function owner() public view virtual returns (address) {
        return _owner;
    }

    function _checkOwner() internal view virtual {
        require(owner() == _msgSender(), "Ownable: caller is not the owner");
    }

    function renounceOwnership() public virtual onlyOwner {
        _transferOwnership(address(0));
    }

    function transferOwnership(address newOwner) public virtual onlyOwner {
        require(newOwner != address(0), "Ownable: new owner is the zero address");
        _transferOwnership(newOwner);
    }

    function _transferOwnership(address newOwner) internal virtual {
        address oldOwner = _owner;
        _owner = newOwner;
        emit OwnershipTransferred(oldOwner, newOwner);
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

abstract contract Pausable is Context {
    event Paused(address account);
    event Unpaused(address account);

    bool private _paused;

    constructor() {
        _paused = false;
    }

    modifier whenNotPaused() {
        _requireNotPaused();
        _;
    }

    modifier whenPaused() {
        _requirePaused();
        _;
    }

    function paused() public view virtual returns (bool) {
        return _paused;
    }

    function _requireNotPaused() internal view virtual {
        require(!paused(), "Pausable: paused");
    }

    function _requirePaused() internal view virtual {
        require(paused(), "Pausable: not paused");
    }

    function _pause() internal virtual whenNotPaused {
        _paused = true;
        emit Paused(_msgSender());
    }

    function _unpause() internal virtual whenPaused {
        _paused = false;
        emit Unpaused(_msgSender());
    }
}

contract DailyTaskRewards is Ownable, ReentrancyGuard, Pausable {
    using SafeERC20 for IERC20;

    IERC20 public immutable goodDollarToken;

    uint256 public rewardAmount;

    uint256 public totalDeposited;
    uint256 public totalDisbursed;
    uint256 public totalWithdrawn;

    mapping(address => uint256) public userTotalRewards;
    mapping(address => uint256) public userRewardCount;
    mapping(bytes32 => bool) public processedTasks;

    event Deposited(address indexed from, uint256 amount, uint256 timestamp);
    event RewardDisbursed(
        address indexed recipient,
        uint256 amount,
        string taskId,
        string platform,
        bytes32 taskHash,
        uint256 timestamp
    );
    event Withdrawn(address indexed to, uint256 amount, uint256 timestamp);
    event RewardAmountUpdated(uint256 oldAmount, uint256 newAmount);
    event EmergencyWithdraw(address indexed to, uint256 amount, uint256 timestamp);

    constructor(
        address _goodDollarToken,
        uint256 _rewardAmount
    ) Ownable(msg.sender) {
        require(_goodDollarToken != address(0), "Invalid token address");
        require(_rewardAmount > 0, "Reward amount must be > 0");

        goodDollarToken = IERC20(_goodDollarToken);
        rewardAmount = _rewardAmount;
    }

    function deposit(uint256 amount) external nonReentrant whenNotPaused {
        require(amount > 0, "Amount must be > 0");

        goodDollarToken.safeTransferFrom(msg.sender, address(this), amount);
        totalDeposited += amount;

        emit Deposited(msg.sender, amount, block.timestamp);
    }

    function depositFrom(address from, uint256 amount) external onlyOwner nonReentrant whenNotPaused {
        require(amount > 0, "Amount must be > 0");
        require(from != address(0), "Invalid from address");

        goodDollarToken.safeTransferFrom(from, address(this), amount);
        totalDeposited += amount;

        emit Deposited(from, amount, block.timestamp);
    }

    function disburseReward(
        address recipient,
        string calldata taskId,
        string calldata platform
    ) external onlyOwner nonReentrant whenNotPaused returns (bytes32) {
        require(recipient != address(0), "Invalid recipient");
        require(bytes(taskId).length > 0, "Invalid task ID");
        require(bytes(platform).length > 0, "Invalid platform");

        bytes32 taskHash = keccak256(abi.encodePacked(recipient, taskId, platform));
        require(!processedTasks[taskHash], "Task reward already disbursed");

        uint256 balance = goodDollarToken.balanceOf(address(this));
        require(balance >= rewardAmount, "Insufficient contract balance");

        processedTasks[taskHash] = true;
        userTotalRewards[recipient] += rewardAmount;
        userRewardCount[recipient] += 1;
        totalDisbursed += rewardAmount;

        goodDollarToken.safeTransfer(recipient, rewardAmount);

        emit RewardDisbursed(recipient, rewardAmount, taskId, platform, taskHash, block.timestamp);

        return taskHash;
    }

    function withdraw(uint256 amount) external onlyOwner nonReentrant {
        require(amount > 0, "Amount must be > 0");

        uint256 balance = goodDollarToken.balanceOf(address(this));
        require(balance >= amount, "Insufficient balance");

        totalWithdrawn += amount;
        goodDollarToken.safeTransfer(owner(), amount);

        emit Withdrawn(owner(), amount, block.timestamp);
    }

    function withdrawAll() external onlyOwner nonReentrant {
        uint256 balance = goodDollarToken.balanceOf(address(this));
        require(balance > 0, "No balance to withdraw");

        totalWithdrawn += balance;
        goodDollarToken.safeTransfer(owner(), balance);

        emit Withdrawn(owner(), balance, block.timestamp);
    }

    function emergencyWithdraw(address token) external onlyOwner nonReentrant {
        IERC20 tokenContract = IERC20(token);
        uint256 balance = tokenContract.balanceOf(address(this));
        require(balance > 0, "No balance");

        tokenContract.safeTransfer(owner(), balance);

        emit EmergencyWithdraw(owner(), balance, block.timestamp);
    }

    function setRewardAmount(uint256 newAmount) external onlyOwner {
        require(newAmount > 0, "Amount must be > 0");

        uint256 oldAmount = rewardAmount;
        rewardAmount = newAmount;

        emit RewardAmountUpdated(oldAmount, newAmount);
    }

    function pause() external onlyOwner {
        _pause();
    }

    function unpause() external onlyOwner {
        _unpause();
    }

    function getContractBalance() external view returns (uint256) {
        return goodDollarToken.balanceOf(address(this));
    }

    function getUserStats(address user) external view returns (
        uint256 totalRewards,
        uint256 rewardCount
    ) {
        return (userTotalRewards[user], userRewardCount[user]);
    }

    function getContractStats() external view returns (
        uint256 balance,
        uint256 deposited,
        uint256 disbursed,
        uint256 withdrawn
    ) {
        return (
            goodDollarToken.balanceOf(address(this)),
            totalDeposited,
            totalDisbursed,
            totalWithdrawn
        );
    }

    function isTaskProcessed(bytes32 taskHash) external view returns (bool) {
        return processedTasks[taskHash];
    }

    function isTaskRewarded(address recipient, string calldata taskId, string calldata platform) external view returns (bool) {
        bytes32 taskHash = keccak256(abi.encodePacked(recipient, taskId, platform));
        return processedTasks[taskHash];
    }

    function getTaskHash(address recipient, string calldata taskId, string calldata platform) external pure returns (bytes32) {
        return keccak256(abi.encodePacked(recipient, taskId, platform));
    }
}"""


def compile_contract():
    """Compile the DailyTaskRewards Solidity contract"""
    logger.info("Installing Solidity compiler v0.8.21...")
    install_solc('0.8.21')

    logger.info("Compiling DailyTaskRewards contract...")

    compiled = compile_standard({
        "language": "Solidity",
        "sources": {
            "DailyTaskRewards.sol": {"content": FLATTENED_SOURCE}
        },
        "settings": {
            "optimizer": {"enabled": True, "runs": 200},
            "outputSelection": {
                "*": {
                    "*": ["abi", "metadata", "evm.bytecode", "evm.deployedBytecode"]
                }
            }
        }
    }, solc_version='0.8.21')

    contract_data = compiled["contracts"]["DailyTaskRewards.sol"]["DailyTaskRewards"]

    return {
        "abi": contract_data["abi"],
        "bytecode": contract_data["evm"]["bytecode"]["object"]
    }


def deploy_contract():
    """Deploy the DailyTaskRewards contract to Celo Mainnet using TASK_KEY"""
    task_key = os.getenv('TASK_KEY')

    if not task_key:
        logger.error("TASK_KEY not set! This is required to deploy the contract.")
        return None

    if not GOODDOLLAR_CONTRACT:
        logger.error("GOODDOLLAR_CONTRACT_ADDRESS not set!")
        return None

    w3 = Web3(Web3.HTTPProvider(CELO_RPC_URL))

    if not w3.is_connected():
        logger.error("Failed to connect to Celo network")
        return None

    logger.info(f"Connected to Celo Mainnet (Chain ID: {CHAIN_ID})")

    if task_key.startswith('0x'):
        account = Account.from_key(task_key)
    else:
        account = Account.from_key('0x' + task_key)

    logger.info(f"Deploying from TASK_KEY address: {account.address}")

    celo_balance = w3.eth.get_balance(account.address)
    logger.info(f"CELO balance: {w3.from_wei(celo_balance, 'ether')} CELO")

    if celo_balance < w3.to_wei(0.1, 'ether'):
        logger.error("Insufficient CELO for gas fees. Top up the TASK_KEY address.")
        return None

    compiled = compile_contract()

    contract = w3.eth.contract(
        abi=compiled["abi"],
        bytecode=compiled["bytecode"]
    )

    logger.info("Building deployment transaction...")

    nonce = w3.eth.get_transaction_count(account.address)
    gas_price = int(w3.eth.gas_price * 1.2)

    constructor_txn = contract.constructor(
        Web3.to_checksum_address(GOODDOLLAR_CONTRACT),
        REWARD_AMOUNT
    ).build_transaction({
        'chainId': CHAIN_ID,
        'gas': 3000000,
        'gasPrice': gas_price,
        'nonce': nonce,
    })

    logger.info("Signing transaction with TASK_KEY...")
    signed_txn = w3.eth.account.sign_transaction(constructor_txn, task_key)

    logger.info("Sending deployment transaction...")
    tx_hash = w3.eth.send_raw_transaction(signed_txn.raw_transaction)
    tx_hash_hex = tx_hash.hex()

    if not tx_hash_hex.startswith('0x'):
        tx_hash_hex = '0x' + tx_hash_hex

    logger.info(f"Transaction hash: {tx_hash_hex}")
    logger.info(f"Explorer: https://celoscan.io/tx/{tx_hash_hex}")

    logger.info("Waiting for confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)

    if receipt.status == 1:
        contract_address = receipt.contractAddress
        logger.info("Contract deployed successfully!")
        logger.info(f"Contract address: {contract_address}")
        logger.info(f"Explorer: https://celoscan.io/address/{contract_address}")
        logger.info(f"Gas used: {receipt.gasUsed}")
        logger.info(f"Block: {receipt.blockNumber}")

        deployment_info = {
            "contract_name": "DailyTaskRewards",
            "contract_address": contract_address,
            "tx_hash": tx_hash_hex,
            "owner": account.address,
            "gooddollar_token": GOODDOLLAR_CONTRACT,
            "reward_amount": str(REWARD_AMOUNT),
            "reward_amount_g": f"{REWARD_AMOUNT / 10**18} G$",
            "chain_id": CHAIN_ID,
            "network": "Celo Mainnet",
            "block_number": receipt.blockNumber,
            "gas_used": receipt.gasUsed,
            "compiler_version": "v0.8.21+commit.d9974bed",
            "optimization": True,
            "optimization_runs": 200,
            "source_code": FLATTENED_SOURCE,
            "abi": compiled["abi"]
        }

        output_path = os.path.join(os.path.dirname(__file__), 'daily_task_deployment_info.json')
        with open(output_path, 'w') as f:
            json.dump(deployment_info, f, indent=2)

        logger.info(f"Deployment info saved to: {output_path}")

        return deployment_info
    else:
        logger.error("Deployment failed!")
        logger.error(f"Transaction: https://celoscan.io/tx/{tx_hash_hex}")
        logger.error(f"Gas used: {receipt.gasUsed}")
        return None


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("DailyTaskRewards Contract Deployment")
    logger.info("=" * 60)
    logger.info(f"Network:           Celo Mainnet (Chain ID: {CHAIN_ID})")
    logger.info(f"GoodDollar Token:  {GOODDOLLAR_CONTRACT}")
    logger.info(f"Reward Amount:     {REWARD_AMOUNT / 10**18} G$ per approved task")
    logger.info(f"Signer:            TASK_KEY wallet")
    logger.info(f"Compiler:          v0.8.21, Optimization: Yes, Runs: 200")
    logger.info("=" * 60)

    result = deploy_contract()

    if result:
        logger.info("\n" + "=" * 60)
        logger.info("DEPLOYMENT SUCCESSFUL!")
        logger.info("=" * 60)
        logger.info(f"\nContract Name:    {result['contract_name']}")
        logger.info(f"Contract Address: {result['contract_address']}")
        logger.info(f"Owner (TASK_KEY): {result['owner']}")
        logger.info(f"Reward Amount:    {result['reward_amount_g']}")
        logger.info(f"\nCeloscan:         https://celoscan.io/address/{result['contract_address']}")
        logger.info(f"\nSet environment variable:")
        logger.info(f"DAILY_TASK_CONTRACT_ADDRESS={result['contract_address']}")
        logger.info("\nVerification Settings (for Celoscan):")
        logger.info("  - Contract Name:  DailyTaskRewards")
        logger.info("  - Compiler:       v0.8.21+commit.d9974bed")
        logger.info("  - Optimization:   Yes")
        logger.info("  - Runs:           200")
        logger.info("  - License:        MIT")
    else:
        logger.error("\nDeployment failed. Check the logs above.")
