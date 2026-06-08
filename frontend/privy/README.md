# Privy Embedded Wallet (POC)

Proof of concept for a MiniPay-style embedded wallet: users log in with email /
Google (no seed phrase, no "Connect MetaMask"), Privy auto-creates a
self-custodial embedded wallet, and they can sign messages / send transactions
without a MetaMask-style approval popup for every action.

## How it's wired

Two bundles are produced by `build.mjs` (esbuild → committed self-contained IIFEs,
like `static/js/wc-bundle.js`, so the Flask app needs no Node build step at runtime):

1. **Standalone POC page** — `src/index.jsx` → `../../static/js/privy-embedded-wallet.js`.
   Mounted by `templates/embedded_wallet.html` at `GET /embedded-wallet` (`main.py`).
2. **Homepage "Create Wallet" bridge** — `src/wallet.jsx` → `../../static/js/privy-wallet.js`.
   Mounts Privy headlessly and exposes an imperative API on `window.GMPrivy`
   (`login()`, `signMessage()`, `getAddress()`, `getProvider()`, `logout()`). The
   homepage (`templates/homepage.html`) lazy-loads it when the user taps
   **Create Wallet**, then reuses the existing `/verify-identity` login flow:
   `GMPrivy.login()` → `GMPrivy.signMessage(loginMessage)` → POST `/verify-identity`
   → redirect `/wallet`.

Both consume `window.__GM_PRIVY_CONFIG__` (Privy App ID + Celo RPC/explorer),
injected by the server.

## Configuration (server env vars)

| Var | Purpose | Default |
|-----|---------|---------|
| `PRIVY_APP_ID` | Privy app client ID (public) — required | _(empty)_ |
| `CELO_RPC_URL` | Celo JSON-RPC endpoint | `https://forno.celo.org` |
| `CELO_EXPLORER_URL` | Block explorer base URL | `https://celoscan.io` |

## Rebuilding the bundle

```bash
cd frontend/privy
npm install
npm run build   # writes both ../../static/js/privy-embedded-wallet.js and privy-wallet.js
```
