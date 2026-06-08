# Privy Embedded Wallet (POC)

Proof of concept for a MiniPay-style embedded wallet: users log in with email /
Google (no seed phrase, no "Connect MetaMask"), Privy auto-creates a
self-custodial embedded wallet, and they can sign messages / send transactions
without a MetaMask-style approval popup for every action.

## How it's wired

- `src/index.jsx` — React component using `@privy-io/react-auth` + `viem`, targeting Celo.
- `build.mjs` — esbuild bundler. Outputs a single self-contained IIFE to
  `../../static/js/privy-embedded-wallet.js` (committed, like `static/js/wc-bundle.js`),
  so the Flask app needs no Node build step at runtime.
- `templates/embedded_wallet.html` — mounts the bundle into `#gmPrivyRoot` and injects
  `window.__GM_PRIVY_CONFIG__` (Privy App ID + Celo RPC/explorer) from the server.
- Flask route: `GET /embedded-wallet` in `main.py`.

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
npm run build   # writes ../../static/js/privy-embedded-wallet.js
```
