// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

interface IGoodMarketRaffleToken {
    function transfer(address to, uint256 amount) external returns (bool);
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function balanceOf(address account) external view returns (uint256);
    function allowance(address owner, address spender) external view returns (uint256);
}

/**
 * @title GoodMarketRaffle
 * @notice Round-based G$ raffle pool.
 *
 * Flow:
 * - A wallet joins the open round by approving and depositing exactly 250 G$.
 * - Each round accepts exactly 400 unique participants.
 * - Once full, deposits are blocked until the randomness provider finalizes winners.
 * - Ten unique winners receive a claimable 10,000 G$ reward each.
 * - Winners pull rewards with withdrawReward(), so distribution cannot be blocked by one wallet.
 *
 * Randomness:
 * - This contract intentionally does not use block.timestamp/blockhash as the source of truth.
 * - Deploy with a trusted/verifiable randomness provider wallet or coordinator.
 * - The provider must call drawWinners(seed) after a round is full.
 */
contract GoodMarketRaffle {
    enum RoundStatus {
        Open,
        Drawing,
        Completed
    }

    uint256 public constant ENTRY_FEE = 250 ether;
    uint16 public constant MAX_PARTICIPANTS = 400;
    uint8 public constant WINNER_COUNT = 10;
    uint256 public constant PRIZE_PER_WINNER = 10_000 ether;

    IGoodMarketRaffleToken public immutable gdToken;
    address public immutable randomnessProvider;
    uint256 public currentRoundId;

    struct Round {
        RoundStatus status;
        address[] participants;
        address[] winners;
        bytes32 randomnessSeed;
        uint256 openedAt;
        uint256 completedAt;
    }

    mapping(uint256 => Round) private rounds;
    mapping(uint256 => mapping(address => bool)) public hasJoined;
    mapping(uint256 => mapping(address => bool)) public isWinner;
    mapping(uint256 => mapping(address => uint256)) public claimableReward;
    mapping(uint256 => mapping(address => bool)) public rewardClaimed;

    event RoundOpened(uint256 indexed roundId, uint256 openedAt);
    event RaffleJoined(uint256 indexed roundId, address indexed participant, uint256 amount, uint256 participantCount);
    event RoundReadyForDraw(uint256 indexed roundId, uint256 participantCount);
    event WinnersDrawn(uint256 indexed roundId, bytes32 indexed randomnessSeed, address[] winners, uint256 prizePerWinner);
    event RewardWithdrawn(uint256 indexed roundId, address indexed winner, uint256 amount);

    modifier onlyRandomnessProvider() {
        require(msg.sender == randomnessProvider, "not_randomness_provider");
        _;
    }

    constructor(address gdTokenAddress, address trustedRandomnessProvider) {
        require(gdTokenAddress != address(0), "zero_gd_token");
        require(trustedRandomnessProvider != address(0), "zero_randomness_provider");

        gdToken = IGoodMarketRaffleToken(gdTokenAddress);
        randomnessProvider = trustedRandomnessProvider;
        currentRoundId = 1;
        rounds[currentRoundId].status = RoundStatus.Open;
        rounds[currentRoundId].openedAt = block.timestamp;
        emit RoundOpened(currentRoundId, block.timestamp);
    }

    function joinRaffle() external returns (bool) {
        Round storage round = rounds[currentRoundId];
        require(round.status == RoundStatus.Open, "round_not_open");
        require(round.participants.length < MAX_PARTICIPANTS, "round_full");
        require(!hasJoined[currentRoundId][msg.sender], "already_joined");

        bool ok = gdToken.transferFrom(msg.sender, address(this), ENTRY_FEE);
        require(ok, "gd_transferfrom_failed");

        hasJoined[currentRoundId][msg.sender] = true;
        round.participants.push(msg.sender);

        emit RaffleJoined(currentRoundId, msg.sender, ENTRY_FEE, round.participants.length);

        if (round.participants.length == MAX_PARTICIPANTS) {
            round.status = RoundStatus.Drawing;
            emit RoundReadyForDraw(currentRoundId, MAX_PARTICIPANTS);
        }

        return true;
    }

    function drawWinners(bytes32 seed) external onlyRandomnessProvider returns (address[] memory winners) {
        Round storage round = rounds[currentRoundId];
        require(round.status == RoundStatus.Drawing, "round_not_ready");
        require(round.participants.length == MAX_PARTICIPANTS, "participants_incomplete");
        require(seed != bytes32(0), "zero_seed");

        uint16[] memory selectedIndexes = new uint16[](WINNER_COUNT);
        winners = new address[](WINNER_COUNT);

        for (uint8 i = 0; i < WINNER_COUNT; i++) {
            uint16 idx = uint16(uint256(keccak256(abi.encode(seed, currentRoundId, i))) % MAX_PARTICIPANTS);

            bool duplicate = true;
            while (duplicate) {
                duplicate = false;
                for (uint8 j = 0; j < i; j++) {
                    if (selectedIndexes[j] == idx) {
                        idx = uint16((idx + 1) % MAX_PARTICIPANTS);
                        duplicate = true;
                        break;
                    }
                }
            }

            selectedIndexes[i] = idx;
            address winner = round.participants[idx];
            winners[i] = winner;
            round.winners.push(winner);
            isWinner[currentRoundId][winner] = true;
            claimableReward[currentRoundId][winner] = PRIZE_PER_WINNER;
        }

        round.randomnessSeed = seed;
        round.status = RoundStatus.Completed;
        round.completedAt = block.timestamp;
        emit WinnersDrawn(currentRoundId, seed, winners, PRIZE_PER_WINNER);

        _openNextRound();
    }

    function withdrawReward(uint256 roundId) external returns (bool) {
        uint256 amount = claimableReward[roundId][msg.sender];
        require(amount > 0, "no_reward");
        require(!rewardClaimed[roundId][msg.sender], "already_claimed");

        rewardClaimed[roundId][msg.sender] = true;
        claimableReward[roundId][msg.sender] = 0;

        bool ok = gdToken.transfer(msg.sender, amount);
        require(ok, "gd_transfer_failed");
        emit RewardWithdrawn(roundId, msg.sender, amount);
        return true;
    }

    function getRound(uint256 roundId) external view returns (
        RoundStatus status,
        uint256 participantCount,
        uint256 winnerCount,
        bytes32 randomnessSeed,
        uint256 openedAt,
        uint256 completedAt
    ) {
        Round storage round = rounds[roundId];
        return (
            round.status,
            round.participants.length,
            round.winners.length,
            round.randomnessSeed,
            round.openedAt,
            round.completedAt
        );
    }

    function getParticipants(uint256 roundId) external view returns (address[] memory) {
        return rounds[roundId].participants;
    }

    function getWinners(uint256 roundId) external view returns (address[] memory) {
        return rounds[roundId].winners;
    }

    function contractBalance() external view returns (uint256) {
        return gdToken.balanceOf(address(this));
    }

    function _openNextRound() internal {
        currentRoundId += 1;
        rounds[currentRoundId].status = RoundStatus.Open;
        rounds[currentRoundId].openedAt = block.timestamp;
        emit RoundOpened(currentRoundId, block.timestamp);
    }
}
