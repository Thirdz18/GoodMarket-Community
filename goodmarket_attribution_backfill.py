"""
GoodMarket Attribution Backfill
================================

Ensures every wallet that face-verified on GoodDollar AND used GoodMarket
(proven by activity in ``goodmarket_claim_facts``) is correctly marked
``verified_after_goodmarket = TRUE`` in ``user_data``.

Why this exists
---------------
The original attribution flow only set ``verified_after_goodmarket = TRUE``
inside ``/fv-callback`` (when GoodDollar redirects back with ``src=goodmarket``).
A user can fall through that net for several legit reasons:

* The user closed the GoodDollar tab before the callback fired.
* A middleware / proxy stripped the ``src=goodmarket`` query param.
* The user verified on GoodDollar before the column even existed.
* The user verified on a separate device, then later started using GoodMarket.

For wallets like ``0x96A868DA...bD99e07c6`` we can see on Celoscan that they
claimed UBI through GoodMarket (the tx originates from our wallet UI), but the
``user_data`` row still has ``verified_after_goodmarket = FALSE``. This module
fixes that gap.

Public surface
--------------
* :func:`mark_verified_via_goodmarket` — idempotent, single-wallet helper.
  Safe to call from any hot path (verify-identity, claim confirm, etc.).
  Wraps all DB / RPC calls in try/except so it NEVER raises.
* :func:`run_full_backfill` — one-shot bulk operation. Walks every wallet
  with rows in ``goodmarket_claim_facts``, on-chain-verifies their FV status,
  and updates stale ``user_data`` rows.
* :func:`init_attribution_backfill` — fire-and-forget startup helper called
  from ``main.py``. Uses a sentinel row in ``goodmarket_attribution_backfill_runs``
  to guarantee one-run-only across multi-worker deploys.

Design rules
------------
* Every public function is best-effort. Failures log a warning and return
  a structured result; they never break the calling flow.
* Source of truth for "is this user FV-verified?" is the on-chain
  ``Identity.isWhitelisted`` check (re-uses ``is_identity_verified`` which
  already has a 5-minute TTL cache, so repeat calls are cheap).
* Source of attribution proof is ``goodmarket_claim_facts`` (any row =
  the user clicked Claim inside our wallet UI).
* All DB writes go through the service-role client when available so RLS
  doesn't silently drop them. Falls back to the anon client otherwise.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Env flag to enable the auto-run-once-on-boot behaviour. Defaults to ON so
# the user's "auto-run on next app boot" requirement is met without any extra
# Vercel env-var step. Set to "0" / "false" to disable.
AUTO_BACKFILL_ENABLED = os.getenv(
    "GOODMARKET_ATTRIBUTION_BACKFILL_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")

# Run key for the sentinel row. Bump this string if you ever need to force
# the auto-backfill to re-run on the next boot (e.g. after schema changes).
AUTO_RUN_KEY = os.getenv(
    "GOODMARKET_ATTRIBUTION_BACKFILL_RUN_KEY", "auto_v1"
).strip() or "auto_v1"

# Cap how many wallets a single run touches so a buggy deploy can't hammer
# the DB / on-chain RPCs. Raise via env var if you really need a bigger run.
MAX_WALLETS_PER_RUN = int(
    os.getenv("GOODMARKET_ATTRIBUTION_BACKFILL_MAX_WALLETS", "5000")
)

# Sleep between on-chain identity checks during the bulk backfill, in
# milliseconds. Keeps us friendly to the public Celo RPC. The identity
# check itself is cached for 5 min, so this only matters on the first pass.
RPC_THROTTLE_MS = int(
    os.getenv("GOODMARKET_ATTRIBUTION_BACKFILL_RPC_THROTTLE_MS", "50")
)

# Delay before kicking off the auto-backfill thread on boot. Gives the rest
# of the app (Supabase client, blockchain helpers) time to fully initialise.
AUTO_RUN_BOOT_DELAY_SECONDS = int(
    os.getenv("GOODMARKET_ATTRIBUTION_BACKFILL_BOOT_DELAY_SECONDS", "30")
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _get_sb_client():
    """Prefer the service-role client so RLS never drops our writes."""
    try:
        from supabase_client import get_supabase_admin_client, get_supabase_client
        sb = get_supabase_admin_client()
        if sb is not None:
            return sb, "service_role"
        sb = get_supabase_client()
        if sb is not None:
            return sb, "anon_fallback"
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[gm-backfill] supabase client lookup failed: {exc}")
    return None, "none"


def _to_checksum(wallet_address: str) -> Optional[str]:
    """Normalise a wallet to EIP-55 checksum. Returns None if invalid."""
    if not wallet_address or not isinstance(wallet_address, str):
        return None
    try:
        from web3 import Web3
        if Web3.is_address(wallet_address):
            return Web3.to_checksum_address(wallet_address)
    except Exception:  # noqa: BLE001
        pass
    return None


def _is_face_verified_on_chain(wallet_address: str) -> Optional[bool]:
    """Return True/False if the on-chain check succeeds, None on RPC error.

    Re-uses the existing 5-min cache in ``blockchain.is_identity_verified``
    so callers can hit this in a tight loop without flooding the RPC.
    """
    try:
        from blockchain import is_identity_verified
        result = is_identity_verified(wallet_address)
        if not isinstance(result, dict):
            return None
        if result.get("error"):
            # On-chain check failed (RPC down, etc.). Don't false-positive.
            return None
        return bool(result.get("verified", False))
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[gm-backfill] is_identity_verified failed for "
            f"{(wallet_address or '')[:10]}...: {exc}"
        )
        return None


# ---------------------------------------------------------------------------
# Single-wallet idempotent helper
# ---------------------------------------------------------------------------

def mark_verified_via_goodmarket(
    wallet_address: str,
    source: str = "unknown",
    *,
    require_on_chain_check: bool = True,
    background: bool = False,
) -> Dict[str, Any]:
    """Mark a wallet as ``verified_after_goodmarket = TRUE`` if appropriate.

    Idempotent. Safe to call from any code path. NEVER raises — all errors
    are caught and returned in the result dict.

    Args:
        wallet_address: The wallet to mark. Will be checksum-normalised.
        source: Free-form tag for logs ("fv_callback", "verify_identity",
            "claim_confirm", "manual_backfill", etc.). Recorded in the
            log message, not the DB.
        require_on_chain_check: When True (default), only flips the flag
            after confirming ``Identity.isWhitelisted == true`` on-chain.
            Set to False ONLY when you already KNOW the user is FV-verified
            (e.g. inside ``/fv-callback`` where GoodDollar just told us so).
        background: When True, the work happens on a daemon thread and the
            return value is the immediate "queued" result. Use this from
            request handlers so we don't add latency to the response.

    Returns:
        ``{"status": "updated"|"already"|"skipped"|"error", "reason": ..., "source": source}``
    """
    if background:
        thread = threading.Thread(
            target=mark_verified_via_goodmarket,
            args=(wallet_address,),
            kwargs={
                "source": source,
                "require_on_chain_check": require_on_chain_check,
                "background": False,
            },
            daemon=True,
            name=f"gm-attr-backfill-{(wallet_address or '')[:8]}",
        )
        thread.start()
        return {"status": "queued", "source": source}

    checksum = _to_checksum(wallet_address)
    if not checksum:
        return {"status": "skipped", "reason": "invalid_address", "source": source}

    sb, client_kind = _get_sb_client()
    if sb is None:
        return {"status": "error", "reason": "no_supabase_client", "source": source}

    # 1. Read current state. ilike() is case-insensitive and matches the
    #    existing pattern used elsewhere in supabase_client.py.
    try:
        row_resp = sb.table("user_data")\
            .select("wallet_address, verified_after_goodmarket, "
                    "first_seen_unverified, ubi_verified, face_verified")\
            .ilike("wallet_address", checksum)\
            .limit(1)\
            .execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[gm-backfill] read user_data failed for {checksum[:10]}... "
            f"(client={client_kind}): {exc}"
        )
        return {"status": "error", "reason": f"read_failed: {exc}", "source": source}

    if not row_resp or not row_resp.data:
        # User isn't in user_data yet — nothing to update. The /verify-identity
        # path creates the row before we get here, so this is rare.
        return {"status": "skipped", "reason": "user_not_in_user_data", "source": source}

    row = row_resp.data[0]

    # 2. Fast-path: already attributed.
    if row.get("verified_after_goodmarket") is True:
        return {"status": "already", "reason": "already_attributed", "source": source}

    # 3. Confirm the user really is face-verified before flipping the flag.
    #    We TRUST face_verified=true in user_data (set previously by
    #    /fv-callback) but still verify on-chain when required, so a stale
    #    DB row can never produce a false positive.
    if require_on_chain_check:
        on_chain = _is_face_verified_on_chain(checksum)
        if on_chain is None:
            # RPC failed — don't risk false-positive, just skip this round.
            return {
                "status": "skipped",
                "reason": "on_chain_check_unavailable",
                "source": source,
            }
        if on_chain is False:
            return {
                "status": "skipped",
                "reason": "not_face_verified_on_chain",
                "source": source,
            }

    # 4. Flip the flag. Also backfill ``first_seen_unverified`` if missing
    #    so analytics queries that expect it don't break.
    update_payload: Dict[str, Any] = {
        "verified_after_goodmarket": True,
    }
    if not row.get("first_seen_unverified"):
        update_payload["first_seen_unverified"] = _now_iso()
    if not row.get("face_verified"):
        update_payload["face_verified"] = True
        update_payload["face_verified_at"] = _now_iso()
    if not row.get("ubi_verified"):
        update_payload["ubi_verified"] = True
        update_payload["verification_timestamp"] = _now_iso()

    try:
        sb.table("user_data")\
            .update(update_payload)\
            .ilike("wallet_address", checksum)\
            .execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[gm-backfill] update user_data failed for {checksum[:10]}... "
            f"(client={client_kind}): {exc}"
        )
        return {"status": "error", "reason": f"update_failed: {exc}", "source": source}

    logger.info(
        f"[gm-backfill] attributed {checksum[:10]}... -> verified_after_goodmarket=TRUE "
        f"(source={source}, client={client_kind})"
    )
    return {"status": "updated", "source": source}


# ---------------------------------------------------------------------------
# Bulk backfill
# ---------------------------------------------------------------------------

def _collect_candidate_wallets(sb) -> List[str]:
    """Pull every distinct wallet that has activity in goodmarket_claim_facts.

    Paginates through the table because Supabase's REST default limit is 1000
    rows. Returns checksum-normalised addresses, de-duplicated.
    """
    seen: Set[str] = set()
    page_size = 1000
    offset = 0

    while True:
        try:
            resp = sb.table("goodmarket_claim_facts")\
                .select("wallet_address")\
                .range(offset, offset + page_size - 1)\
                .execute()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[gm-backfill] failed to read goodmarket_claim_facts "
                f"page offset={offset}: {exc}"
            )
            break

        rows = resp.data or []
        if not rows:
            break

        for row in rows:
            raw = (row or {}).get("wallet_address")
            checksum = _to_checksum(raw) if raw else None
            if checksum:
                seen.add(checksum)
            elif raw:
                # Address didn't validate — keep the original lowercase form
                # so we still try to update; ilike is case-insensitive.
                seen.add(str(raw).strip())

        if len(rows) < page_size:
            break
        offset += page_size

        if len(seen) >= MAX_WALLETS_PER_RUN:
            logger.info(
                f"[gm-backfill] candidate cap reached "
                f"({MAX_WALLETS_PER_RUN}); stopping pagination"
            )
            break

    return sorted(seen)


def run_full_backfill(dry_run: bool = False, limit: Optional[int] = None) -> Dict[str, Any]:
    """One-shot backfill across every GoodMarket-claim wallet.

    For each candidate wallet:
        * Look up the ``user_data`` row.
        * Skip if ``verified_after_goodmarket`` is already TRUE.
        * Verify on-chain ``Identity.isWhitelisted`` (cached 5 min).
        * If verified, flip the flag (or just count when ``dry_run=True``).

    Args:
        dry_run: When True, no writes happen. Returns the same shape so the
            admin endpoint can preview the impact.
        limit: Hard cap on candidates examined this run. Defaults to
            ``MAX_WALLETS_PER_RUN``. Useful for chunked manual runs.

    Returns:
        Structured summary with counts + a sample of updated wallets.
    """
    started_at = time.time()
    sb, client_kind = _get_sb_client()
    if sb is None:
        return {
            "success": False,
            "error": "no_supabase_client",
            "dry_run": dry_run,
        }

    cap = min(int(limit), MAX_WALLETS_PER_RUN) if limit else MAX_WALLETS_PER_RUN
    candidates = _collect_candidate_wallets(sb)[:cap]
    logger.info(
        f"[gm-backfill] full run started: dry_run={dry_run} "
        f"candidates={len(candidates)} client={client_kind}"
    )

    examined = 0
    already = 0
    updated = 0
    skipped_no_user = 0
    skipped_not_verified = 0
    skipped_rpc = 0
    errors = 0
    updated_sample: List[str] = []

    for wallet in candidates:
        examined += 1
        try:
            # Read current state.
            row_resp = sb.table("user_data")\
                .select("wallet_address, verified_after_goodmarket, "
                        "first_seen_unverified, ubi_verified, face_verified")\
                .ilike("wallet_address", wallet)\
                .limit(1)\
                .execute()
            if not row_resp or not row_resp.data:
                skipped_no_user += 1
                continue
            row = row_resp.data[0]

            if row.get("verified_after_goodmarket") is True:
                already += 1
                continue

            on_chain = _is_face_verified_on_chain(wallet)
            if on_chain is None:
                skipped_rpc += 1
                continue
            if on_chain is False:
                skipped_not_verified += 1
                continue

            if dry_run:
                updated += 1
                if len(updated_sample) < 50:
                    updated_sample.append(wallet)
                continue

            update_payload: Dict[str, Any] = {"verified_after_goodmarket": True}
            if not row.get("first_seen_unverified"):
                update_payload["first_seen_unverified"] = _now_iso()
            if not row.get("face_verified"):
                update_payload["face_verified"] = True
                update_payload["face_verified_at"] = _now_iso()
            if not row.get("ubi_verified"):
                update_payload["ubi_verified"] = True
                update_payload["verification_timestamp"] = _now_iso()

            sb.table("user_data")\
                .update(update_payload)\
                .ilike("wallet_address", wallet)\
                .execute()

            updated += 1
            if len(updated_sample) < 50:
                updated_sample.append(wallet)
            logger.info(
                f"[gm-backfill] FULL attributed {wallet[:10]}... "
                f"-> verified_after_goodmarket=TRUE"
            )
        except Exception as exc:  # noqa: BLE001
            errors += 1
            logger.warning(
                f"[gm-backfill] error processing {wallet[:10] if wallet else '?'}...: {exc}"
            )

        if RPC_THROTTLE_MS > 0:
            time.sleep(RPC_THROTTLE_MS / 1000.0)

    duration_seconds = round(time.time() - started_at, 2)
    summary = {
        "success": True,
        "dry_run": dry_run,
        "client": client_kind,
        "candidates": len(candidates),
        "examined": examined,
        "updated": updated,
        "already_attributed": already,
        "skipped_no_user_row": skipped_no_user,
        "skipped_not_face_verified": skipped_not_verified,
        "skipped_rpc_error": skipped_rpc,
        "errors": errors,
        "updated_sample": updated_sample,
        "duration_seconds": duration_seconds,
    }
    logger.info(f"[gm-backfill] full run finished: {summary}")
    return summary


# ---------------------------------------------------------------------------
# Auto-run-once on boot (with multi-worker safety via sentinel row)
# ---------------------------------------------------------------------------

def _claim_run_slot(sb, run_key: str) -> bool:
    """Try to insert a sentinel row for this run. Returns True if we got it.

    Relies on a UNIQUE constraint on ``run_key`` to make the insert atomic
    across workers. The first worker wins; everyone else hits the unique
    violation and skips.

    If the table doesn't exist yet (admin hasn't run the SQL migration),
    we log a clear hint and return False so we don't blow up the boot path.
    """
    try:
        sb.table("goodmarket_attribution_backfill_runs").insert({
            "run_key": run_key,
            "started_at": _now_iso(),
            "status": "running",
        }).execute()
        return True
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "duplicate" in msg or "unique" in msg or "already" in msg:
            logger.info(
                f"[gm-backfill] sentinel row for run_key={run_key} already exists — "
                f"another worker (or a previous boot) handled it; skipping."
            )
            return False
        if "does not exist" in msg or "relation" in msg:
            logger.warning(
                "[gm-backfill] table goodmarket_attribution_backfill_runs is missing. "
                "Run sql/goodmarket_attribution_backfill.sql in the Supabase SQL editor "
                "to enable auto-run-once-on-boot."
            )
            return False
        logger.warning(f"[gm-backfill] sentinel insert failed: {exc}")
        return False


def _finalise_run_slot(sb, run_key: str, summary: Dict[str, Any]) -> None:
    """Update the sentinel row with the final summary. Best-effort."""
    try:
        sb.table("goodmarket_attribution_backfill_runs")\
            .update({
                "completed_at": _now_iso(),
                "status": "completed" if summary.get("success") else "errored",
                "wallets_examined": int(summary.get("examined") or 0),
                "wallets_updated": int(summary.get("updated") or 0),
                "errors": int(summary.get("errors") or 0),
                "notes": (
                    f"candidates={summary.get('candidates')} "
                    f"already={summary.get('already_attributed')} "
                    f"skipped_no_user={summary.get('skipped_no_user_row')} "
                    f"skipped_not_verified={summary.get('skipped_not_face_verified')} "
                    f"skipped_rpc={summary.get('skipped_rpc_error')} "
                    f"duration_s={summary.get('duration_seconds')}"
                )[:500],
            })\
            .eq("run_key", run_key)\
            .execute()
    except Exception as exc:  # noqa: BLE001
        logger.warning(f"[gm-backfill] sentinel finalise failed: {exc}")


def _auto_backfill_worker() -> None:
    """Background thread entry point. Sleeps briefly so the rest of the app
    is fully up before we start hitting Supabase / the RPC."""
    try:
        if AUTO_RUN_BOOT_DELAY_SECONDS > 0:
            time.sleep(AUTO_RUN_BOOT_DELAY_SECONDS)

        sb, _ = _get_sb_client()
        if sb is None:
            logger.warning("[gm-backfill] auto-run aborted: no supabase client")
            return

        if not _claim_run_slot(sb, AUTO_RUN_KEY):
            return

        try:
            summary = run_full_backfill(dry_run=False)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[gm-backfill] auto-run crashed: {exc}")
            summary = {"success": False, "error": str(exc), "examined": 0,
                       "updated": 0, "errors": 1}

        _finalise_run_slot(sb, AUTO_RUN_KEY, summary)
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[gm-backfill] auto-run worker fatal: {exc}")


def init_attribution_backfill(app: Any = None) -> bool:
    """Spawn the auto-run-once worker on app boot. Returns True if started.

    Mirrors the opt-in pattern used by ``init_goodmarket_claim_reconciler``
    so it only runs in long-lived processes. The sentinel row in
    ``goodmarket_attribution_backfill_runs`` makes it safe to call this
    from every Gunicorn worker — only one will actually do the work.
    """
    if not AUTO_BACKFILL_ENABLED:
        logger.info(
            "[gm-backfill] auto-run disabled "
            "(set GOODMARKET_ATTRIBUTION_BACKFILL_ENABLED=1 to enable)"
        )
        return False

    try:
        thread = threading.Thread(
            target=_auto_backfill_worker,
            name="gm-attribution-auto-backfill",
            daemon=True,
        )
        thread.start()
        logger.info(
            f"[gm-backfill] auto-run thread scheduled "
            f"(delay={AUTO_RUN_BOOT_DELAY_SECONDS}s, run_key={AUTO_RUN_KEY})"
        )
        return True
    except Exception as exc:  # noqa: BLE001
        logger.error(f"[gm-backfill] failed to start auto-run thread: {exc}")
        return False
