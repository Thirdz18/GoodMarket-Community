# Deploying `GoodMarketMiniPayCUSDFaucet` in Remix

## What this contract does
- Holds cUSD inside the contract.
- Allows one fixed disburser wallet (set at deploy) to disburse cUSD to users.
- Emits custom event `GoodMarketTopWallet` on every disbursement.
- **Gas is still paid in CELO by the caller wallet** (your backend signer / `TOPWALLET_KEY`).

## 1) Open Remix and compile
1. Go to https://remix.ethereum.org
2. Create/import file: `GoodMarketMiniPayCUSDFaucet.sol`
3. Solidity compiler version: **0.8.20**
4. Compile contract.

> ⚠️ **Common mistake:** `deploy_minipay_cusd_faucet.py` is a Python file.  
> Do not paste it into a `.sol` file in Remix, or it will show red syntax errors.

## 2) Deploy constructor params
Constructor:
- `cUSDToken`: `0x765DE816845861e75A25fCA122bb6898B8B1282a` (Celo mainnet cUSD)
- `fixedDisburser`: backend wallet address that will call `disburseCUSD` (usually `TOPWALLET_KEY` public address)
- `fixedCooldownSeconds`: per-wallet on-chain cooldown (recommended: `172800` for 48h)

Network:
- Celo Mainnet (chainId `42220`)

## 3) Fund the contract
After deploy, transfer cUSD into contract address (this is the faucet pool).

## 4) Disburse call format
Function:
- `disburseCUSD(recipient, amount, correlationId, sourceTag)`

Example:
- `recipient`: `0xabc...`
- `amount`: `10000000000000000` for `0.01 cUSD` (18 decimals)
- `correlationId`: bytes32 like `0x6661756365742d31323300000000000000000000000000000000000000000000`
- `sourceTag`: `minipay_cusd_faucet`

## 5) Event logging
Every successful disbursement emits:
- `GoodMarketTopWallet(recipient, operator, amount, correlationId, sourceTag, timestamp)`

This gives you custom on-chain analytics/audit naming.


## 6) Backend env vars after you deploy in Remix
Once you have the deployed contract address, set these in your GoodMarket app env:

- `GOODMARKETFAUCETMODE=CONTRACT` to use contract-based disbursement
- `GOODMARKET_CUSD_FAUCET_CONTRACT_ADDRESS=<your deployed contract address>`
- `TOPWALLET_KEY=<same backend private key>`
- `CUSD_CONTRACT=0x765DE816845861e75A25fCA122bb6898B8B1282a` (Celo mainnet)

Fallback / legacy mode:

- `GOODMARKETFAUCETMODE=PRIVATEKEY`
- In this mode, backend sends `cUSD.transfer(...)` directly from `TOPWALLET_KEY`.

### Mode behavior summary
- `CONTRACT`: backend uses `TOPWALLET_KEY` to call `disburseCUSD(...)` on your faucet contract.
- `PRIVATEKEY`: backend uses `TOPWALLET_KEY` to call cUSD token `transfer(...)` directly.
- In both modes, **gas fee is paid in CELO by the TOPWALLET_KEY signer**.


## 7) Deposit behavior (requested)
- Anyone can deposit cUSD into the faucet pool.
- Recommended: call `approve(faucetAddress, amount)` on cUSD, then call `depositCUSD(amount)` on faucet contract.
- You can still send cUSD directly via `cUSD.transfer(faucetAddress, amount)` as normal ERC-20 transfer.
- There is no withdraw/emergency-withdraw/admin function in the faucet contract.
- Cooldown is also enforced on-chain via `disburseCUSD` and `cooldownRemaining(recipient)`.

## 8) If Remix shows red error / Gas estimation failed
Quick checks:
- Make sure your opened file is **Solidity contract** (`GoodMarketMiniPayCUSDFaucet.sol`), not the Python deploy script.
- Re-compile with Solidity compiler `0.8.20`.
- Constructor fields must be:
  - `cUSDToken = 0x765DE816845861e75A25fCA122bb6898B8B1282a`
  - `fixedDisburser = <public address of TOPWALLET_KEY signer>`
  - `fixedCooldownSeconds = 172800` (must be > 0)
- Deploy `Value` must be `0`.
