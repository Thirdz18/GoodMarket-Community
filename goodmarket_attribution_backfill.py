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

# Window (seconds) for the strict attribution rule. The on-chain
# ``lastAuthenticated`` timestamp must fall within this many seconds of when
# GoodMarket's ``/fv-callback`` recorded ``face_verified_at`` (or of "now" for
# live writes) for the attribution to count. Any wider gap means the wallet
# was almost certainly verified through a different dApp and just round-tripped
# back through GoodMarket's FV button afterwards. Tunable via env var so we
# can loosen it without a redeploy if RPC indexing latency ever spikes.
STRICT_ATTRIBUTION_WINDOW_SECONDS = int(
    os.getenv("GOODMARKET_ATTRIBUTION_STRICT_WINDOW_SECONDS", str(30 * 60))
)

# Master switch for the strict attribution rule. Default ON. Flip to "0" to
# fall back to the old "any whitelisted user counts" behaviour without needing
# a redeploy (e.g. if a buggy on-chain RPC is causing every write to skip).
STRICT_ATTRIBUTION_ENABLED = os.getenv(
    "GOODMARKET_ATTRIBUTION_STRICT_ENABLED", "1"
).strip().lower() in ("1", "true", "yes", "on")


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


def _get_on_chain_last_authenticated(wallet_address: str) -> Optional[int]:
    """Return the on-chain ``lastAuthenticated`` unix timestamp, or None on RPC error.

    Re-uses the 5-minute cache inside ``blockchain.get_identity_expiry`` so
    callers can poll cheaply. Returns ``0`` if the wallet has *never* been
    authenticated on-chain (the contract returns 0).
    """
    try:
        from blockchain import get_identity_expiry
        result = get_identity_expiry(wallet_address)
        if not isinstance(result, dict):
            return None
        if not result.get("success"):
            return None
        # ``date_authenticated`` is the unix timestamp of the most recent
        # on-chain authentication tx (lastAuthenticated, with a fallback to
        # dateAuthenticated for older deployments). 0 means never authenticated.
        return int(result.get("date_authenticated", 0) or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            f"[gm-attribution] get_identity_expiry failed for "
            f"{(wallet_address or '')[:10]}...: {exc}"
        )
        return None


def _parse_iso_to_unix(value: Any) -> Optional[int]:
    """Parse an ISO-8601 timestamp string to a unix-seconds int. ``None`` on failure."""
    if not value:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if not isinstance(value, str):
        return None
    try:
        # Tolerate trailing "Z" (UTC) and missing tz (assume UTC).
        normalised = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(normalised)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except Exception:  # noqa: BLE001
        return None


def is_attributable_to_goodmarket(
    wallet_address: str,
    sb_row: Optional[Dict[str, Any]] = None,
    *,
    reference_unix: Optional[int] = None,
    window_seconds: Optional[int] = None,
    on_chain_last_auth: Optional[int] = None,
) -> Dict[str, Any]:
    """Strict attribution check used by every write path.

    Returns a structured decision dict so callers can both branch on the
    boolean ``attributable`` field AND log/persist the reason. The dict is
    intentionally easy to JSON-serialize for the admin correction endpoint.

    The two conditions for a TRUE attribution:

    1. The wallet's on-chain ``lastAuthenticated`` timestamp is **after** the
       row's ``first_login`` (the user came to GoodMarket *before* they
       verified anywhere). If ``first_login`` is missing we fall back to
       ``first_seen_unverified`` and then ``created_at``; if all three are
       missing we cannot prove attribution and reject.

    2. The on-chain ``lastAuthenticated`` falls within
       ``STRICT_ATTRIBUTION_WINDOW_SECONDS`` of ``reference_unix`` (defaults
       to ``face_verified_at`` for already-written rows, or ``time.time()``
       for live ``/fv-callback`` writes). This excludes the "verified weeks
       ago elsewhere, just now round-tripped through GM's FV button" pattern.

    When ``STRICT_ATTRIBUTION_ENABLED`` is False we fall back to the legacy
    "is whitelisted on-chain" rule so operators can disable the strict check
    via env var without a redeploy.

    Args:
        wallet_address: Wallet to check. Will be checksum-normalised.
        sb_row: The ``user_data`` row dict (must contain ``first_login``,
            ``first_seen_unverified``, ``face_verified_at``, ``created_at``).
            Pass ``None`` to skip the timing comparison entirely (useful for
            unit tests or pre-DB code paths).
        reference_unix: Override the "now" reference for the closeness check.
            Defaults to ``face_verified_at`` (if present) or current time.
        window_seconds: Override ``STRICT_ATTRIBUTION_WINDOW_SECONDS``.
        on_chain_last_auth: Override the on-chain lookup. When None the
            function performs a fresh ``get_identity_expiry`` call.

    Returns:
        ``{
            "attributable": bool,
            "reason": str,                 # short machine-readable code
            "last_authenticated_unix": int | None,
            "first_login_unix": int | None,
            "reference_unix": int | None,
            "delta_seconds": int | None,   # |last_auth - reference|
        }``
    """
    out: Dict[str, Any] = {
        "attributable": False,
        "reason": "unknown",
        "last_authenticated_unix": None,
        "first_login_unix": None,
        "reference_unix": None,
        "delta_seconds": None,
    }

    if not STRICT_ATTRIBUTION_ENABLED:
        # Legacy permissive behaviour — anyone whitelisted on-chain is OK.
        verified = _is_face_verified_on_chain(wallet_address)
        if verified is None:
            out["reason"] = "on_chain_check_unavailable"
            return out
        if not verified:
            out["reason"] = "not_face_verified_on_chain"
            return out
        out["attributable"] = True
        out["reason"] = "strict_disabled_legacy_pass"
        return out

    last_auth = on_chain_last_auth
    if last_auth is None:
        last_auth = _get_on_chain_last_authenticated(wallet_address)
    out["last_authenticated_unix"] = last_auth

    if last_auth is None:
        out["reason"] = "on_chain_check_unavailable"
        return out
    if last_auth <= 0:
        out["reason"] = "never_authenticated_on_chain"
        return out

    row = sb_row or {}

    first_login_unix = (
        _parse_iso_to_unix(row.get("first_login"))
        or _parse_iso_to_unix(row.get("first_seen_unverified"))
        or _parse_iso_to_unix(row.get("created_at"))
    )
    out["first_login_unix"] = first_login_unix

    if first_login_unix is None:
        out["reason"] = "no_first_login_timestamp"
        return out

    if last_auth < first_login_unix:
        out["reason"] = "verified_before_first_login"
        return out

    # Closeness check.
    if reference_unix is None:
        reference_unix = (
            _parse_iso_to_unix(row.get("face_verified_at"))
            or int(time.time())
        )
    out["reference_unix"] = reference_unix

    delta = abs(last_auth - reference_unix)
    out["delta_seconds"] = delta

    window = int(window_seconds if window_seconds is not None else STRICT_ATTRIBUTION_WINDOW_SECONDS)
    if delta > window:
        out["reason"] = "verification_outside_goodmarket_session"
        return out

    out["attributable"] = True
    out["reason"] = "ok"
    return out


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
                    "first_seen_unverified, ubi_verified, face_verified, "
                    "face_verified_at, first_login, created_at")\
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

    # 3. Strict attribution check. Replaces the prior "is whitelisted on-chain?"
    #    check, which was producing false positives — anyone whitelisted who
    #    later round-tripped through GoodMarket's FV button would get the flag
    #    even if their actual face verification happened months earlier on a
    #    different dApp. ``is_attributable_to_goodmarket`` enforces both
    #    "came to GM before verifying" AND "verified during this GM session"
    #    (within STRICT_ATTRIBUTION_WINDOW_SECONDS of the reference timestamp).
    if require_on_chain_check:
        decision = is_attributable_to_goodmarket(checksum, row)
        if not decision["attributable"]:
            return {
                "status": "skipped",
                "reason": decision["reason"],
                "source": source,
                "attribution_decision": decision,
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
    skipped_not_attributable = 0
    errors = 0
    updated_sample: List[str] = []
    skipped_attribution_reasons: Dict[str, int] = {}

    for wallet in candidates:
        examined += 1
        try:
            # Read current state.
            row_resp = sb.table("user_data")\
                .select("wallet_address, verified_after_goodmarket, "
                        "first_seen_unverified, ubi_verified, face_verified, "
                        "face_verified_at, first_login, created_at")\
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

            decision = is_attributable_to_goodmarket(wallet, row)
            if not decision["attributable"]:
                reason = decision.get("reason", "unknown")
                if reason == "on_chain_check_unavailable":
                    skipped_rpc += 1
                elif reason in ("not_face_verified_on_chain", "never_authenticated_on_chain"):
                    skipped_not_verified += 1
                else:
                    skipped_not_attributable += 1
                skipped_attribution_reasons[reason] = (
                    skipped_attribution_reasons.get(reason, 0) + 1
                )
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
        "skipped_not_attributable": skipped_not_attributable,
        "skipped_attribution_reasons": skipped_attribution_reasons,
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


# ---------------------------------------------------------------------------
# Strict-rule correction for already-stored false positives
# ---------------------------------------------------------------------------

def correct_false_attributions(
    dry_run: bool = True,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """Re-evaluate every ``user_data`` row that currently has
    ``verified_after_goodmarket = TRUE`` against the strict attribution rule
    and unset the flag where the row no longer qualifies.

    Designed to be invoked manually via the admin endpoint
    ``/api/admin/attribution-correct`` (dry-run by default). It NEVER auto-runs.
    Stricter than ``run_full_backfill`` because here we *remove* attributions
    that were granted under the old loose rule, so we want a human in the loop.

    Returns a summary identical in shape to ``run_full_backfill`` for
    consistency, plus a ``cleared_sample`` list of wallets that were (or
    would be) unset.
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

    try:
        rows_resp = sb.table("user_data")\
            .select("wallet_address, verified_after_goodmarket, "
                    "first_seen_unverified, ubi_verified, face_verified, "
                    "face_verified_at, first_login, created_at")\
            .eq("verified_after_goodmarket", True)\
            .limit(cap)\
            .execute()
    except Exception as exc:  # noqa: BLE001
        return {
            "success": False,
            "error": f"read_failed: {exc}",
            "dry_run": dry_run,
        }

    rows = list(rows_resp.data or [])
    examined = 0
    cleared = 0
    kept = 0
    skipped_rpc = 0
    cleared_sample: List[Dict[str, Any]] = []
    reasons: Dict[str, int] = {}

    for row in rows:
        examined += 1
        wallet_raw = (row or {}).get("wallet_address")
        wallet = _to_checksum(wallet_raw) or wallet_raw
        if not wallet:
            continue

        decision = is_attributable_to_goodmarket(wallet, row)
        if decision["attributable"]:
            kept += 1
            continue

        reason = decision.get("reason", "unknown")
        reasons[reason] = reasons.get(reason, 0) + 1
        if reason == "on_chain_check_unavailable":
            # Don't clear when the RPC is sad — could come back True next time.
            skipped_rpc += 1
            continue

        if dry_run:
            cleared += 1
            if len(cleared_sample) < 100:
                cleared_sample.append({"wallet": wallet, "reason": reason,
                                       "decision": decision})
            continue

        try:
            sb.table("user_data")\
                .update({"verified_after_goodmarket": False})\
                .ilike("wallet_address", wallet)\
                .execute()
            cleared += 1
            if len(cleared_sample) < 100:
                cleared_sample.append({"wallet": wallet, "reason": reason,
                                       "decision": decision})
            logger.info(
                f"[gm-attribution] cleared false-positive attribution "
                f"for {wallet[:10]}... reason={reason}"
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                f"[gm-attribution] failed to clear {wallet[:10]}...: {exc}"
            )

        if RPC_THROTTLE_MS > 0:
            time.sleep(RPC_THROTTLE_MS / 1000.0)

    duration_seconds = round(time.time() - started_at, 2)
    return {
        "success": True,
        "dry_run": dry_run,
        "client": client_kind,
        "examined": examined,
        "cleared": cleared,
        "kept_genuine": kept,
        "skipped_rpc_unavailable": skipped_rpc,
        "reasons": reasons,
        "cleared_sample": cleared_sample,
        "duration_seconds": duration_seconds,
    }
