"""
P2P auto-expiry + reconciliation worker.

Two responsibilities, both server-side safety nets so the feature "just works"
without external cron infra (mirrors learn_and_earn/stream_scheduler.py and
goodmarket_claim_reconciler.py):

1. **Auto-expiry** — when a buyer opens an order they reserve G$ on-chain for a
   payment window (default 30 min). If they never mark-paid, that G$ stays
   locked. This worker finds Open orders past their deadline and calls the
   permissionless ``cancelOrder`` (signed with P2P_KEY for gas), which returns
   the reserved G$ to the listing's available balance, then mirrors the
   ``cancelled`` status into Supabase.

2. **Reconciliation** — for active (open|paid) orders, pull the on-chain order
   status and fix any Supabase row that drifted (e.g. the wallet UI fired the
   release/cancel tx but never called back). On-chain is the source of truth.

Each Gunicorn worker spawns its own daemon thread. Concurrent runs are safe:
``cancelOrder`` reverts for an already-cancelled/non-Open order, so a lost race
just wastes a little gas — it never double-spends. Gated by
``P2P_EXPIRY_WORKER_ENABLED`` (defaults ON when both P2P_KEY and the contract
address are configured, OFF otherwise).
"""

from __future__ import annotations
from env_utils import get_env_float, get_env_int

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = get_env_int("P2P_EXPIRY_WORKER_INTERVAL_SECONDS", 120)
DEFAULT_EXPIRE_BATCH = get_env_int("P2P_EXPIRY_WORKER_BATCH", 50)
DEFAULT_RECONCILE_BATCH = get_env_int("P2P_RECONCILE_WORKER_BATCH", 100)
BOOT_DELAY_SECONDS = get_env_int("P2P_EXPIRY_WORKER_BOOT_DELAY_SECONDS", 25)

# On-chain terminal/active statuses we map back into Supabase during reconcile.
_ONCHAIN_TO_DB = {
    "released": "released",
    "cancelled": "cancelled",
    "disputed": "disputed",
}


def _scheduler_enabled() -> bool:
    """ON by default when the contract + owner key are configured; override with
    ``P2P_EXPIRY_WORKER_ENABLED`` (1/0)."""
    override = os.getenv("P2P_EXPIRY_WORKER_ENABLED", "").strip().lower()
    if override in ("1", "true", "yes", "on"):
        return True
    if override in ("0", "false", "no", "off"):
        return False
    from . import blockchain as chain

    return bool(chain.is_configured() and chain.P2P_KEY)


def process_expiries_once(expire_limit: int = DEFAULT_EXPIRE_BATCH) -> Dict[str, Any]:
    """Cancel + refund Open orders whose deadline has passed. Returns a summary."""
    from . import blockchain as chain
    from . import db

    summary = {"checked": 0, "expired": 0, "skipped": 0, "failed": 0}
    if not (chain.is_configured() and chain.P2P_KEY):
        return summary

    now_iso = datetime.now(timezone.utc).isoformat()
    rows = db.list_expired_open_orders(now_iso=now_iso, limit=expire_limit)
    for row in rows:
        summary["checked"] += 1
        order_id = row.get("id")
        onchain_id = row.get("onchain_id")
        if onchain_id is None:
            summary["skipped"] += 1
            continue

        # Trust on-chain: only cancel if it is still Open and actually expired.
        live = chain.get_order(onchain_id)
        if not live:
            summary["skipped"] += 1
            continue
        if live.get("status") != "open":
            # Already moved on-chain; mirror it and skip the cancel tx.
            db.update_order(order_id, {"status": _ONCHAIN_TO_DB.get(live.get("status"), row["status"])})
            summary["skipped"] += 1
            continue
        if live.get("deadline", 0) >= int(datetime.now(timezone.utc).timestamp()):
            summary["skipped"] += 1
            continue

        try:
            result = chain.cancel_expired_order(onchain_id)
            if result.get("success"):
                db.update_order(order_id, {
                    "status": "cancelled",
                    "reject_reason": "auto-expired: buyer did not pay within the window",
                })
                summary["expired"] += 1
                logger.info("[p2p-expiry] cancelled expired order id=%s onchain=%s tx=%s",
                            order_id, onchain_id, result.get("tx_hash"))
            else:
                summary["failed"] += 1
                logger.warning("[p2p-expiry] cancel tx reverted order id=%s onchain=%s tx=%s",
                               order_id, onchain_id, result.get("tx_hash"))
        except Exception as exc:  # noqa: BLE001
            summary["failed"] += 1
            logger.exception("[p2p-expiry] cancel failed order id=%s: %s", order_id, exc)
    return summary


def reconcile_orders_once(reconcile_limit: int = DEFAULT_RECONCILE_BATCH) -> Dict[str, Any]:
    """Sync Supabase order status with on-chain for active (open|paid) orders."""
    from . import blockchain as chain
    from . import db

    summary = {"checked": 0, "updated": 0}
    if not chain.is_configured():
        return summary

    rows = db.list_open_or_paid_orders(limit=reconcile_limit)
    for row in rows:
        summary["checked"] += 1
        onchain_id = row.get("onchain_id")
        if onchain_id is None:
            continue
        live = chain.get_order(onchain_id)
        if not live:
            continue
        mapped = _ONCHAIN_TO_DB.get(live.get("status"))
        # Only push terminal/dispute states; never override an admin-set status
        # like seller_rejected/owner_released/owner_refunded.
        if mapped and mapped != row.get("status") and row.get("status") in ("open", "paid"):
            db.update_order(row["id"], {"status": mapped})
            summary["updated"] += 1
            logger.info("[p2p-reconcile] order id=%s %s -> %s (on-chain)",
                        row["id"], row.get("status"), mapped)
    return summary


class P2PExpiryScheduler:
    """Periodic worker: auto-expire stale orders + reconcile on-chain status."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.poll_interval = max(30, DEFAULT_INTERVAL_SECONDS)
        self.expire_batch = DEFAULT_EXPIRE_BATCH
        self.reconcile_batch = DEFAULT_RECONCILE_BATCH
        self._last_run_at: Optional[str] = None
        self._last_run_summary: Dict[str, Any] = {}
        self._total_expired = 0
        self._total_reconciled = 0
        self._total_failed = 0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.info("[p2p-expiry] already running")
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever, name="p2p-expiry-worker", daemon=True
        )
        self._thread.start()
        logger.info("[p2p-expiry] started poll=%ss expire_batch=%s reconcile_batch=%s",
                    self.poll_interval, self.expire_batch, self.reconcile_batch)

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _run_forever(self) -> None:
        self._stop.wait(BOOT_DELAY_SECONDS)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[p2p-expiry] cycle crashed: %s", exc)
            self._stop.wait(self.poll_interval)

    def run_once(self) -> Dict[str, Any]:
        expiry = process_expiries_once(self.expire_batch)
        reconcile = reconcile_orders_once(self.reconcile_batch)
        summary = {"expiry": expiry, "reconcile": reconcile}
        self._last_run_at = datetime.now(timezone.utc).isoformat()
        self._last_run_summary = summary
        self._total_expired += int(expiry.get("expired") or 0)
        self._total_reconciled += int(reconcile.get("updated") or 0)
        self._total_failed += int(expiry.get("failed") or 0)
        if expiry.get("expired") or expiry.get("failed") or reconcile.get("updated"):
            logger.info("[p2p-expiry] cycle: %s", summary)
        return summary

    def get_status(self) -> Dict[str, Any]:
        return {
            "running": self.is_running(),
            "poll_interval_seconds": self.poll_interval,
            "last_run_at": self._last_run_at,
            "last_run_summary": self._last_run_summary,
            "total_expired": self._total_expired,
            "total_reconciled": self._total_reconciled,
            "total_failed": self._total_failed,
        }


_scheduler: Optional[P2PExpiryScheduler] = None
_scheduler_lock = threading.Lock()


def get_expiry_scheduler() -> P2PExpiryScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = P2PExpiryScheduler()
    return _scheduler


def init_p2p_expiry_scheduler(app: Any = None) -> bool:
    """Start the in-process P2P expiry/reconcile worker if enabled."""
    if not _scheduler_enabled():
        logger.info("[p2p-expiry] scheduler disabled "
                    "(P2P contract/key not configured or P2P_EXPIRY_WORKER_ENABLED=0)")
        return False
    try:
        get_expiry_scheduler().start()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception("[p2p-expiry] failed to start: %s", exc)
        return False
