"""
GoodMarket Claim Reconciler
============================

Backend reconciliation layer for ``goodmarket_claim_facts``.

Why this exists
---------------
``/api/claims/v2/confirm`` writes whatever ``status`` the browser reports
("submitted" while the tx is still pending, "confirmed" after the wallet
receipt callback fires). If the user closes the page (or the in-app wallet
silently drops the receipt event — common on MiniPay / Trust mobile) the
row stays at ``status='submitted'`` and ``confirmed_at IS NULL`` forever,
even when the tx clearly succeeded on-chain. Those rows never roll into
the ``goodmarket_unique_claimers`` KPI which only counts ``status='confirmed'``.

This module reconciles those rows against the actual chain receipts:

* A periodic background worker scans rows where ``status='submitted'``
  and ``confirmed_at IS NULL`` and fetches each tx receipt via the same
  Celo / XDC RPCs the rest of the app already uses.
* On receipt success → row flips to ``status='confirmed'``,
  ``verification_state='verified'``, ``confirmed_at=now()``.
* On receipt failure → row flips to ``status='failed'``,
  ``verification_state='verified'``.
* If no receipt has appeared after ``RETRY_WINDOW_HOURS`` we mark the row
  ``status='unknown'`` and stop polling it, so the worker doesn't loop
  forever on tx hashes that never landed.
* Every status transition appends an idempotent row to
  ``goodmarket_claim_events`` (the unique index on
  ``(claim_attempt_id, event_type, tx_hash)`` swallows duplicates).

There are no UI / wallet.html changes. The KPI query
(``status='confirmed'`` only) keeps working — the reconciler just makes
sure stuck rows eventually flip to ``confirmed``.

Public surface
--------------
* :func:`reconcile_one` — one-shot reconciliation for a single ``tx_hash``.
  Used by the periodic worker AND by the admin
  ``POST /api/claims/v2/reconcile-one`` endpoint.
* :class:`GoodMarketClaimReconciler` — background polling worker.
* :func:`get_reconciler` — singleton accessor.
* :func:`init_goodmarket_claim_reconciler` — opt-in start helper called
  from ``main.py``. Gated by ``GOODMARKET_CLAIM_RECONCILER_ENABLED``.
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from web3 import Web3

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Config (env-overridable)
# ---------------------------------------------------------------------------

# How often the background worker wakes up.
DEFAULT_POLL_INTERVAL_SECONDS = int(os.getenv("GOODMARKET_RECONCILER_INTERVAL", "30"))

# Max rows reconciled per cycle. Each row is a single eth_getTransactionReceipt
# RPC call, so this also bounds outbound RPC traffic.
DEFAULT_BATCH_SIZE = int(os.getenv("GOODMARKET_RECONCILER_BATCH_SIZE", "50"))

# Bounded backoff: don't keep polling a tx hash forever — give up and mark
# 'unknown' after this many hours. The user's claim won't count toward
# unique_claimers but it also won't remain a perpetually open work item.
RETRY_WINDOW_HOURS = int(os.getenv("GOODMARKET_RECONCILER_RETRY_HOURS", "24"))

# Throttle: a row must not have been touched within this many seconds
# before we re-check its receipt. Each touch (even "still pending") bumps
# updated_at, so this acts as a per-row cooldown without us needing a
# dedicated ``next_check_at`` column.
MIN_RECHECK_SECONDS = int(os.getenv("GOODMARKET_RECONCILER_MIN_RECHECK_SECONDS", "20"))

# RPC URLs — fall back to the same defaults blockchain.py uses.
_CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
# erpc.xinfin.network has been returning 502s; default to a known-good public
# endpoint. The XDC client below also prefers blockchain.get_xdc_rpc() which
# fails over across several providers.
_XDC_RPC = os.getenv("XDC_RPC_URL", "https://earpc.xinfin.network")

# RPC request timeout (seconds). receipts on Celo are usually <1s; XDC's
# public RPC occasionally returns HTML errors and we want to fail fast.
_RPC_TIMEOUT = int(os.getenv("GOODMARKET_RECONCILER_RPC_TIMEOUT", "12"))


# ---------------------------------------------------------------------------
# Web3 helpers
# ---------------------------------------------------------------------------

_w3_cache: Dict[str, Web3] = {}
_w3_cache_lock = threading.Lock()


def _get_w3(network: str) -> Optional[Web3]:
    """Return a cached Web3 client for the given network, or None."""
    network = (network or "").lower()
    if network not in ("celo", "xdc"):
        return None
    with _w3_cache_lock:
        cached = _w3_cache.get(network)
        if cached is not None:
            return cached
        if network == "celo":
            url = _CELO_RPC
        else:
            # Prefer blockchain.py's failover-aware resolver for XDC so a dead
            # provider (e.g. erpc.xinfin.network 502) doesn't stall reconciliation.
            url = _XDC_RPC
            if not os.getenv("XDC_RPC_URL"):
                try:
                    import blockchain as _bc
                    url = _bc.get_xdc_rpc()
                except Exception:  # noqa: BLE001
                    url = _XDC_RPC
        try:
            w3 = Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": _RPC_TIMEOUT}))
            _w3_cache[network] = w3
            return w3
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "[gm-reconciler] could not init Web3 for network=%s url=%s: %s",
                network, url, exc,
            )
            return None


# ---------------------------------------------------------------------------
# Time / supabase helpers
# ---------------------------------------------------------------------------

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat()


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        normalized = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(normalized) if isinstance(normalized, str) else None
        if not dt:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _normalize_tx_hash(tx_hash: str) -> str:
    return (tx_hash or "").strip().lower()


def _get_supabase():
    """Service-role client preferred (RLS write bypass); anon as fallback."""
    from supabase_client import get_supabase_admin_client, get_supabase_client

    sb = get_supabase_admin_client()
    if sb is None:
        sb = get_supabase_client()
        if sb is not None:
            logger.warning(
                "[gm-reconciler] SUPABASE_SERVICE_ROLE_KEY not set — using anon "
                "client. Updates will fail if RLS is enabled on facts/events tables."
            )
    return sb


def _append_event(
    sb: Any,
    *,
    claim_attempt_id: Optional[str],
    wallet: Optional[str],
    network: str,
    tx_hash: str,
    event_type: str,
    error_code: Optional[str] = None,
    error_message: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    """Append a row to ``goodmarket_claim_events``.

    Best-effort: the unique index ``uq_gm_claim_events_idempotency`` is
    expected to swallow duplicate transitions on retry. We never block the
    fact-table update on event logging.
    """
    if not claim_attempt_id or not wallet:
        # claim_attempt_id is NOT NULL in the table; without it we can't log.
        # This only happens for legacy rows recorded before the v2 schema —
        # safe to skip silently.
        return
    try:
        sb.table("goodmarket_claim_events").insert({
            "claim_attempt_id": claim_attempt_id,
            "wallet_address": wallet,
            "network": network,
            "tx_hash": tx_hash,
            "event_type": event_type,
            # source is CHECKed (length 1..64). "goodmarket_reconciler" makes
            # it easy to filter reconciler-driven transitions in BI later.
            "source": "goodmarket_reconciler",
            "error_code": error_code,
            "error_message": error_message,
            "metadata": metadata or {},
            "created_at": _utcnow_iso(),
        }).execute()
    except Exception as exc:  # noqa: BLE001
        # Idempotency unique-violation hits this path on retry — debug only.
        logger.debug("[gm-reconciler] event insert skipped: %s", exc)


# ---------------------------------------------------------------------------
# Core: reconcile_one
# ---------------------------------------------------------------------------

def reconcile_one(
    tx_hash: str,
    network: str,
    sb: Any = None,
) -> Dict[str, Any]:
    """Reconcile a single ``goodmarket_claim_facts`` row against the chain.

    Idempotent — match key is ``tx_hash`` (lowercased). Safe to call
    repeatedly: rows already at ``confirmed`` / ``failed`` / ``rejected``
    short-circuit with ``no_change=True``.

    Returns a dict like::

        {"success": True, "status": "confirmed", "block_number": 12345}
        {"success": True, "status": "submitted", "receipt": "pending"}
        {"success": True, "status": "unknown", "reason": "timeout"}
        {"success": False, "error": "..."}
    """
    tx_hash = _normalize_tx_hash(tx_hash)
    network = (network or "").lower()

    if not tx_hash.startswith("0x") or len(tx_hash) < 10:
        return {"success": False, "error": "Invalid tx_hash"}
    if network not in ("celo", "xdc"):
        return {"success": False, "error": "Invalid network"}

    sb = sb or _get_supabase()
    if sb is None:
        return {"success": False, "error": "Storage unavailable"}

    # Fetch the row. We match on lowercase tx_hash because
    # /api/claims/v2/confirm normalizes to lowercase before insert; the
    # column has a UNIQUE constraint, so at most one row exists.
    try:
        existing_resp = (
            sb.table("goodmarket_claim_facts")
            .select(
                "id, wallet_address, network, tx_hash, status, "
                "verification_state, claim_attempt_id, correlation_id, "
                "submitted_at, confirmed_at, created_at, updated_at"
            )
            .eq("tx_hash", tx_hash)
            .limit(1)
            .execute()
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[gm-reconciler] facts read failed for %s: %s", tx_hash, exc)
        return {"success": False, "error": f"DB read failed: {exc}"}

    if not existing_resp.data:
        return {
            "success": False,
            "error": "tx_hash not found in goodmarket_claim_facts",
        }

    row = existing_resp.data[0]
    current_status = (row.get("status") or "").lower()

    # Terminal states — nothing to do.
    if current_status in ("confirmed", "failed", "rejected"):
        return {
            "success": True,
            "no_change": True,
            "status": current_status,
            "row_id": row.get("id"),
        }

    w3 = _get_w3(network)
    if w3 is None:
        return {"success": False, "error": f"No RPC configured for {network}"}

    # Fetch receipt. web3.py raises TransactionNotFound when the tx is
    # still pending or unknown to this RPC node — both surfaces the same
    # way to us (treat as "not found yet").
    receipt = None
    try:
        receipt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc).lower()
        if "not found" in msg or "no transaction" in msg or "transactionnotfound" in msg:
            receipt = None
        else:
            logger.warning(
                "[gm-reconciler] receipt fetch failed tx=%s network=%s: %s",
                tx_hash, network, exc,
            )
            # Bump updated_at so we don't hammer this row inside the same cycle.
            try:
                sb.table("goodmarket_claim_facts").update(
                    {"updated_at": _utcnow_iso()}
                ).eq("id", row["id"]).execute()
            except Exception:
                pass
            return {"success": False, "error": f"Receipt fetch failed: {exc}"}

    now_iso = _utcnow_iso()

    # ── Receipt still pending ────────────────────────────────────────────
    if receipt is None:
        created_at = _parse_dt(row.get("created_at")) or _parse_dt(row.get("submitted_at"))
        too_old = (
            created_at is not None
            and (_utcnow() - created_at) > timedelta(hours=RETRY_WINDOW_HOURS)
        )
        if too_old:
            try:
                sb.table("goodmarket_claim_facts").update({
                    "status": "unknown",
                    # Keep verification_state='pending' to signal we never
                    # saw a receipt — distinct from a verified failure.
                    "verification_state": "pending",
                    "updated_at": now_iso,
                }).eq("id", row["id"]).execute()
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "[gm-reconciler] mark-unknown update failed tx=%s: %s",
                    tx_hash, exc,
                )
                return {"success": False, "error": f"Mark unknown failed: {exc}"}

            _append_event(
                sb,
                claim_attempt_id=row.get("claim_attempt_id"),
                wallet=row.get("wallet_address"),
                network=network,
                tx_hash=tx_hash,
                event_type="claim_tx_failed",
                error_code="receipt_timeout",
                error_message=f"No receipt after {RETRY_WINDOW_HOURS}h",
                metadata={"reason": "reconciler_timeout"},
            )
            logger.info(
                "[gm-reconciler] marked unknown tx=%s… age>%sh",
                tx_hash[:12], RETRY_WINDOW_HOURS,
            )
            return {"success": True, "status": "unknown", "reason": "timeout"}

        # Still within retry window — bump updated_at so the next cycle's
        # cooldown filter (lt updated_at) gives this row time to settle.
        try:
            sb.table("goodmarket_claim_facts").update(
                {"updated_at": now_iso}
            ).eq("id", row["id"]).execute()
        except Exception as exc:  # noqa: BLE001
            logger.debug(
                "[gm-reconciler] cooldown bump failed tx=%s: %s", tx_hash, exc,
            )
        return {"success": True, "status": "submitted", "receipt": "pending"}

    # ── Receipt found ────────────────────────────────────────────────────
    block_number = receipt.get("blockNumber")
    try:
        block_number = int(block_number) if block_number is not None else None
    except (TypeError, ValueError):
        block_number = None

    receipt_status = receipt.get("status")
    try:
        receipt_status = int(receipt_status) if receipt_status is not None else None
    except (TypeError, ValueError):
        receipt_status = None

    # Best-effort: pull tx_from / tx_to so analytics can later correlate
    # with the expected claim contract. Failure here doesn't block update.
    tx_from: Optional[str] = None
    tx_to: Optional[str] = None
    try:
        tx = w3.eth.get_transaction(tx_hash)
        raw_from = tx.get("from") if tx is not None else None
        raw_to = tx.get("to") if tx is not None else None
        if raw_from:
            tx_from = str(raw_from).lower()
        if raw_to:
            tx_to = str(raw_to).lower()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[gm-reconciler] tx fetch failed tx=%s: %s", tx_hash, exc)

    if receipt_status == 1:
        # SUCCESS — flip to confirmed. Preserve the existing submitted_at
        # if we had one; if not, treat created_at as the submit timestamp.
        update = {
            "status": "confirmed",
            "verification_state": "verified",
            "confirmed_at": now_iso,
            "updated_at": now_iso,
            "block_number": block_number,
            "tx_from": tx_from,
            "tx_to": tx_to,
        }
        if not row.get("submitted_at"):
            update["submitted_at"] = row.get("created_at") or now_iso

        try:
            sb.table("goodmarket_claim_facts").update(update).eq(
                "id", row["id"]
            ).execute()
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[gm-reconciler] confirm update failed tx=%s: %s", tx_hash, exc,
            )
            return {"success": False, "error": f"Confirm update failed: {exc}"}

        _append_event(
            sb,
            claim_attempt_id=row.get("claim_attempt_id"),
            wallet=row.get("wallet_address"),
            network=network,
            tx_hash=tx_hash,
            event_type="claim_tx_confirmed",
            metadata={"block_number": block_number, "via": "reconciler"},
        )
        logger.info(
            "[gm-reconciler] confirmed tx=%s… network=%s block=%s",
            tx_hash[:12], network, block_number,
        )
        return {
            "success": True,
            "status": "confirmed",
            "block_number": block_number,
            "row_id": row.get("id"),
        }

    # FAILED on-chain (status==0 or anything other than 1).
    update = {
        "status": "failed",
        "verification_state": "verified",
        "updated_at": now_iso,
        "block_number": block_number,
        "tx_from": tx_from,
        "tx_to": tx_to,
    }
    try:
        sb.table("goodmarket_claim_facts").update(update).eq(
            "id", row["id"]
        ).execute()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[gm-reconciler] failed-update failed tx=%s: %s", tx_hash, exc,
        )
        return {"success": False, "error": f"Failed update failed: {exc}"}

    _append_event(
        sb,
        claim_attempt_id=row.get("claim_attempt_id"),
        wallet=row.get("wallet_address"),
        network=network,
        tx_hash=tx_hash,
        event_type="claim_tx_failed",
        error_code="receipt_status_0",
        error_message="On-chain receipt status=0 (revert)",
        metadata={
            "block_number": block_number,
            "via": "reconciler",
            "receipt_status": receipt_status,
        },
    )
    logger.info(
        "[gm-reconciler] failed tx=%s… network=%s block=%s receipt_status=%s",
        tx_hash[:12], network, block_number, receipt_status,
    )
    return {
        "success": True,
        "status": "failed",
        "block_number": block_number,
        "row_id": row.get("id"),
    }


# ---------------------------------------------------------------------------
# Background worker
# ---------------------------------------------------------------------------

class GoodMarketClaimReconciler:
    """Polling worker that reconciles ``submitted`` rows against the chain."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.poll_interval = DEFAULT_POLL_INTERVAL_SECONDS
        self.batch_size = DEFAULT_BATCH_SIZE
        # Lightweight stats for /api/claims/v2/reconciler-status
        self._last_run_at: Optional[str] = None
        self._last_run_summary: Dict[str, Any] = {}
        self._total_confirmed = 0
        self._total_failed = 0
        self._total_unknown = 0

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.info("[gm-reconciler] already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            name="gm-claim-reconciler",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "[gm-reconciler] started poll=%ss batch=%s retry_window=%sh",
            self.poll_interval, self.batch_size, RETRY_WINDOW_HOURS,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- main loop -------------------------------------------------------

    def _run_forever(self) -> None:
        # Stagger first run slightly so multiple Gunicorn workers don't all
        # hit the DB and RPC at the same instant on cold-boot.
        self._stop.wait(min(self.poll_interval, 5))
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[gm-reconciler] cycle crashed: %s", exc)
            self._stop.wait(self.poll_interval)

    def run_once(self) -> Dict[str, Any]:
        """Single reconciliation cycle. Safe to invoke ad-hoc."""
        sb = _get_supabase()
        if sb is None:
            summary = {"reason": "no_supabase", "checked": 0}
            self._record_run(summary)
            return summary

        # Cooldown: only re-check rows last touched more than
        # MIN_RECHECK_SECONDS ago. Each pending bump rewrites updated_at,
        # so this naturally spreads RPC calls across cycles.
        cutoff_iso = (_utcnow() - timedelta(seconds=MIN_RECHECK_SECONDS)).isoformat()

        try:
            resp = (
                sb.table("goodmarket_claim_facts")
                .select("id, tx_hash, network, status, created_at, updated_at, confirmed_at")
                .eq("status", "submitted")
                .is_("confirmed_at", "null")
                .lt("updated_at", cutoff_iso)
                .order("updated_at", desc=False)
                .limit(self.batch_size)
                .execute()
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("[gm-reconciler] fetch pending failed: %s", exc)
            summary = {"error": str(exc)[:160], "checked": 0}
            self._record_run(summary)
            return summary

        rows = resp.data or []
        if not rows:
            summary = {"checked": 0}
            self._record_run(summary)
            return summary

        confirmed = failed_count = unknown = still_pending = errors = 0

        for r in rows:
            tx_hash = r.get("tx_hash")
            network = r.get("network")
            try:
                result = reconcile_one(tx_hash, network, sb=sb)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                logger.exception(
                    "[gm-reconciler] reconcile_one crashed tx=%s: %s",
                    tx_hash, exc,
                )
                continue

            if not result.get("success"):
                errors += 1
                continue

            status = result.get("status")
            if status == "confirmed":
                confirmed += 1
            elif status == "failed":
                failed_count += 1
            elif status == "unknown":
                unknown += 1
            elif status == "submitted":
                still_pending += 1

        summary = {
            "checked": len(rows),
            "confirmed": confirmed,
            "failed": failed_count,
            "unknown": unknown,
            "still_pending": still_pending,
            "errors": errors,
        }
        self._total_confirmed += confirmed
        self._total_failed += failed_count
        self._total_unknown += unknown
        self._record_run(summary)
        if confirmed or failed_count or unknown:
            logger.info("[gm-reconciler] cycle: %s", summary)
        return summary

    # ---- diagnostics -----------------------------------------------------

    def _record_run(self, summary: Dict[str, Any]) -> None:
        self._last_run_at = _utcnow_iso()
        self._last_run_summary = summary

    def status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running(),
            "poll_interval_s": self.poll_interval,
            "batch_size": self.batch_size,
            "retry_window_hours": RETRY_WINDOW_HOURS,
            "min_recheck_seconds": MIN_RECHECK_SECONDS,
            "last_run_at": self._last_run_at,
            "last_run_summary": self._last_run_summary,
            "totals": {
                "confirmed": self._total_confirmed,
                "failed": self._total_failed,
                "unknown": self._total_unknown,
            },
        }


# ---------------------------------------------------------------------------
# Singleton + init helper
# ---------------------------------------------------------------------------

_reconciler: Optional[GoodMarketClaimReconciler] = None
_reconciler_lock = threading.Lock()


def get_reconciler() -> GoodMarketClaimReconciler:
    global _reconciler
    with _reconciler_lock:
        if _reconciler is None:
            _reconciler = GoodMarketClaimReconciler()
    return _reconciler


def init_goodmarket_claim_reconciler(app: Any = None) -> bool:
    """Start the reconciler if ``GOODMARKET_CLAIM_RECONCILER_ENABLED`` is set.

    Mirrors the opt-in pattern used by ``init_p2p_trading`` so the worker
    only spins up in real long-lived processes (Gunicorn / Reserved VM)
    and stays out of unit tests, one-shot CLI invocations, or local dev
    runs that don't want background threads.
    """
    enabled = os.getenv(
        "GOODMARKET_CLAIM_RECONCILER_ENABLED", ""
    ).strip().lower() in ("1", "true", "yes", "on")
    if not enabled:
        logger.info(
            "[gm-reconciler] disabled "
            "(set GOODMARKET_CLAIM_RECONCILER_ENABLED=1 to enable)"
        )
        return False
    try:
        get_reconciler().start()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("[gm-reconciler] failed to start: %s", exc)
        return False
