# UBI Gas Fallback Test Checklist

- [ ] **Enough gas direct claim**
  - Wallet already has CELO >= `required_gas_wei`.
  - `/api/faucet/status` returns `status=gas_ready` and `gas_ready=true`.
  - Frontend proceeds directly to claim without faucet wait loop.

- [ ] **API faucet success**
  - `/api/faucet/gas` API call returns accepted with `txHash`.
  - Balance increases within grace window.
  - Response returns `status=gas_ready`, `topup_source=api`.
  - Cooldown is recorded only after confirmed credit.

- [ ] **API accepted but no credit -> on-chain fallback**
  - API returns `ok=1` but missing `txHash` **or** no balance increase after grace.
  - Backend auto-runs on-chain fallback.
  - Response returns `status=onchain_sent` (or `gas_ready` once funded), with fallback debug info.

- [ ] **Recent refill blocked**
  - Wallet has a recent successful top-up in duplicate window.
  - `/api/faucet/gas` returns `status=recent_refill`, `gas_ready=false`, and cooldown remaining.
  - Frontend stops polling and shows cooldown-based actionable message.

- [ ] **On-chain not configured**
  - `GAMES_KEY` missing/empty in environment.
  - On-chain fallback returns `status=onchain_failed` with `reason=not_configured`.
  - UI shows clear retry/support guidance.

- [ ] **User-facing errors**
  - Polling extends with backoff for up to ~3 minutes.
  - On timeout, UI shows support instruction including wallet/debug context recommendation.
  - No private key is exposed client-side; wallet authorization is session-validated server-side.
