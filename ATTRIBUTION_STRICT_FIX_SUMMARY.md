# Strict GoodMarket Attribution Fix

## Problem

Audit of the 131 wallets currently flagged `verified_after_goodmarket = TRUE`
in `user_data` (May 6, 2026):

| Category | Count | % |
|---|---:|---:|
| **GENUINE** (verified during a GoodMarket session) | **2** | **1.6%** |
| PRE_VERIFIED (verified BEFORE first GoodMarket login) | 42 | 34.1% |
| POST_VERIFIED_AFTER_LOGIN (verified via OTHER service) | 75 | 61.0% |
| NEVER_WHITELISTED | 1 | 0.8% |
| NO_FV_TIMESTAMP | 3 | 2.4% |
| **Total (deduped)** | **123** | |

**Only 1.6% of "verified via GoodMarket" wallets actually verified through
GoodMarket.** The other 98.4% are false positives produced by three
attribution paths that flipped the flag for any whitelisted user:

1. `/fv-callback` (in `supabase_client.log_verification_attempt`) —
   flipped the flag whenever GoodID redirected back with `isVerified=true`,
   even if the user verified months ago elsewhere and just round-tripped.
2. `mark_verified_via_goodmarket` (in
   `goodmarket_attribution_backfill.py`) — flipped the flag for any
   whitelisted user with claim activity.
3. `run_full_backfill` (auto-runs on boot) — scanned every wallet in
   `goodmarket_claim_facts`, flipped the flag for anyone whitelisted.

## Fix

Replaces the legacy "is whitelisted on-chain?" check with a strict
attribution rule shared by all three paths plus the new admin-triggered
correction tool.

### Strict rule (`is_attributable_to_goodmarket`)

A wallet's face verification is attributable to GoodMarket iff BOTH:

1. On-chain `lastAuthenticated` is **after** the wallet's first GoodMarket
   touchpoint (`first_login`, falling back to `first_seen_unverified`,
   falling back to `created_at`). If all three are missing we cannot
   prove attribution → reject.
2. On-chain `lastAuthenticated` falls within
   `STRICT_ATTRIBUTION_WINDOW_SECONDS` (default **30 minutes**) of the
   reference timestamp (`face_verified_at` for already-stored rows, or
   `time.time()` for live `/fv-callback` writes).

The 30-minute window is generous enough to absorb GoodID redirect lag and
RPC indexing delay, tight enough to exclude users who verified weeks ago
elsewhere. Tunable via env var without a redeploy:
`GOODMARKET_ATTRIBUTION_STRICT_WINDOW_SECONDS`.

Master kill-switch: `GOODMARKET_ATTRIBUTION_STRICT_ENABLED=0` reverts to
the legacy "is whitelisted on-chain?" rule.

### Touchpoints

| File | Change |
|---|---|
| `goodmarket_attribution_backfill.py` | Added `is_attributable_to_goodmarket`, `_get_on_chain_last_authenticated`, `_parse_iso_to_unix`, `correct_false_attributions`. Tightened `mark_verified_via_goodmarket` and `run_full_backfill`. |
| `supabase_client.py` | `log_verification_attempt` now consults the strict helper before flipping `verified_after_goodmarket`. |
| `routes.py` | New admin endpoint `POST /api/admin/attribution-correct` (dry-run by default) for backfilling existing false positives. |
| `tests/test_attribution_strict.py` | 15 unit tests covering GENUINE, PRE_VERIFIED, POST_VERIFIED, never-authenticated, missing timestamp, fallback to `created_at`, custom window, and legacy-fallback when strict is disabled. |

### Backfill correction (existing 121 false positives)

The new admin endpoint walks every row with `verified_after_goodmarket =
TRUE`, re-evaluates each against the strict rule, and unsets the flag for
rows that don't qualify. **Dry-run by default** so the operator can preview
the impact before writing.

```sh
# Preview impact (no writes):
curl -X POST 'https://goodmarket.live/api/admin/attribution-correct?dry_run=1' \
  --cookie "session=<your admin session>"

# Apply corrections (writes verified_after_goodmarket=FALSE for false positives):
curl -X POST 'https://goodmarket.live/api/admin/attribution-correct?dry_run=0' \
  --cookie "session=<your admin session>"
```

Audit log is written to `admin_action_logs` for every invocation.

## Operational notes

- **No data is destroyed.** The correction endpoint only flips
  `verified_after_goodmarket` from `TRUE` to `FALSE` — `face_verified`,
  `ubi_verified`, `face_verified_at`, and `verification_timestamp` are
  preserved. If we discover a regression, the next scheduled run of the
  forward attribution path will re-evaluate and re-flag genuinely
  attributable wallets.
- **RPC errors don't false-clear.** If the on-chain check returns
  `on_chain_check_unavailable`, the row is left untouched and counted
  under `skipped_rpc_unavailable` in the response.
- **Metric impact (after correction):** the all-time
  `verified_after_goodmarket = TRUE` count drops from 131 → ~2 (the
  GENUINE category). New verifications that legitimately happen through
  GoodMarket from now on will count correctly.
