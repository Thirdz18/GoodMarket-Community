"""
Learn & Earn Stream Scheduler
=============================

In-process background worker that periodically settles Learn & Earn streaming
payouts on-chain via Superfluid CFA.

Why this exists
---------------
The quiz submit path queues a row in ``learn_earn_streams`` with status
``pending_start`` when ``LEARN_EARN_PAYOUT_MODE`` is one of the streaming
aliases (``stream_1day`` / ``stream`` / ``streaming`` / ``stream_payout``).
Without a worker driving those rows forward they sit forever, and the user
never actually receives any G$ — the quiz submit returns a fake
``queued:<id>`` tx_hash and nothing else happens.

The original design relied on an external cron job hitting
``POST /learn-earn/process-streams`` with a bearer token. That works but
adds an operational surface (cron infra, shared token rotation, network
availability between cron and app). For the GoodMarket community deploy
we want streaming to "just work" once the env vars and DB migration are in
place — no extra external infra.

This module starts a daemon thread inside every Gunicorn worker that wakes
up every ``LEARN_EARN_STREAM_WORKER_INTERVAL_SECONDS`` and calls the same
``streaming_service.process_streams_once`` function the HTTP endpoint uses.
Multiple workers racing on the same row is safe because
``process_streams_once`` claims each row with an optimistic-concurrency
update (``WHERE id=? AND status=? AND retry_count=?``) before submitting
the on-chain transaction. The on-chain ``createFlow`` / ``deleteFlow`` are
themselves idempotent — duplicate calls revert without changing state —
so even if claims slip the failure mode is wasted gas on the duplicate,
not a double-streamed reward.

Mirrors the existing pattern used by ``goodmarket_claim_reconciler.py``.

Public surface
--------------
* :class:`LearnEarnStreamScheduler` — periodic worker.
* :func:`get_stream_scheduler` — singleton accessor.
* :func:`init_learn_earn_stream_scheduler` — opt-in start helper called
  from ``main.py``. Gated by ``LEARN_EARN_STREAM_SCHEDULER_ENABLED``
  (defaults ON whenever ``LEARN_EARN_PAYOUT_MODE`` is a streaming alias,
  otherwise OFF — so flipping the payout mode alone is enough to enable
  the worker, with an explicit kill-switch still available).
"""

from __future__ import annotations

import logging
import os
import threading
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


DEFAULT_INTERVAL_SECONDS = int(os.getenv('LEARN_EARN_STREAM_WORKER_INTERVAL_SECONDS', '120'))
DEFAULT_START_BATCH = int(os.getenv('LEARN_EARN_STREAM_WORKER_START_BATCH', '50'))
DEFAULT_STOP_BATCH = int(os.getenv('LEARN_EARN_STREAM_WORKER_STOP_BATCH', '100'))
BOOT_DELAY_SECONDS = int(os.getenv('LEARN_EARN_STREAM_WORKER_BOOT_DELAY_SECONDS', '20'))


def _streaming_mode_enabled() -> bool:
    """Match the alias set in learn_and_earn.STREAMING_MODE_ALIASES."""
    mode = (os.getenv('LEARN_EARN_PAYOUT_MODE', 'instant') or 'instant').strip().lower()
    return mode in {'stream_1day', 'stream', 'streaming', 'stream_payout'}


def _scheduler_enabled() -> bool:
    """Resolve the scheduler's effective enabled flag.

    Default: ON when ``LEARN_EARN_PAYOUT_MODE`` is a streaming alias, OFF
    otherwise. ``LEARN_EARN_STREAM_SCHEDULER_ENABLED`` overrides either way
    (set to ``1`` to force on, ``0`` to force off).
    """
    override = os.getenv('LEARN_EARN_STREAM_SCHEDULER_ENABLED', '').strip().lower()
    if override in ('1', 'true', 'yes', 'on'):
        return True
    if override in ('0', 'false', 'no', 'off'):
        return False
    return _streaming_mode_enabled()


class LearnEarnStreamScheduler:
    """Periodic worker that drives queued stream rows to on-chain settlement."""

    def __init__(self) -> None:
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.poll_interval = max(15, DEFAULT_INTERVAL_SECONDS)
        self.start_batch = DEFAULT_START_BATCH
        self.stop_batch = DEFAULT_STOP_BATCH
        self._last_run_at: Optional[str] = None
        self._last_run_summary: Dict[str, Any] = {}
        self._total_started = 0
        self._total_stopped = 0
        self._total_failed = 0

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            logger.info('[learn-earn-stream] already running')
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run_forever,
            name='learn-earn-stream-worker',
            daemon=True,
        )
        self._thread.start()
        logger.info(
            '[learn-earn-stream] started poll=%ss start_batch=%s stop_batch=%s',
            self.poll_interval, self.start_batch, self.stop_batch,
        )

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)

    def is_running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ---- main loop -------------------------------------------------------

    def _run_forever(self) -> None:
        # Stagger first run so multiple Gunicorn workers don't all hammer
        # Supabase and the Celo RPC at the same instant on cold-boot.
        self._stop.wait(BOOT_DELAY_SECONDS)
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception as exc:  # noqa: BLE001
                logger.exception('[learn-earn-stream] cycle crashed: %s', exc)
            self._stop.wait(self.poll_interval)

    def run_once(self) -> Dict[str, Any]:
        """Single processing cycle. Safe to invoke ad-hoc."""
        from .learn_and_earn import streaming_service

        from datetime import datetime, timezone
        summary = streaming_service.process_streams_once(
            start_limit=self.start_batch,
            stop_limit=self.stop_batch,
        )
        self._last_run_at = datetime.now(timezone.utc).isoformat()
        self._last_run_summary = summary
        self._total_started += int(summary.get('started') or 0)
        self._total_stopped += int(summary.get('stopped') or 0)
        self._total_failed += int(summary.get('failed') or 0)
        if (summary.get('started') or summary.get('stopped') or summary.get('failed')):
            logger.info('[learn-earn-stream] cycle: %s', summary)
        return summary

    def get_status(self) -> Dict[str, Any]:
        return {
            'running': self.is_running(),
            'poll_interval_seconds': self.poll_interval,
            'last_run_at': self._last_run_at,
            'last_run_summary': self._last_run_summary,
            'total_started': self._total_started,
            'total_stopped': self._total_stopped,
            'total_failed': self._total_failed,
        }


_scheduler: Optional[LearnEarnStreamScheduler] = None
_scheduler_lock = threading.Lock()


def get_stream_scheduler() -> LearnEarnStreamScheduler:
    global _scheduler
    with _scheduler_lock:
        if _scheduler is None:
            _scheduler = LearnEarnStreamScheduler()
    return _scheduler


def init_learn_earn_stream_scheduler(app: Any = None) -> bool:
    """Start the in-process stream worker if streaming is enabled.

    Returns True when the worker thread is spawned. Multiple Gunicorn workers
    each spawn their own thread; per-row OCC claims in
    ``streaming_service.process_streams_once`` keep concurrent runs safe.
    """
    if not _scheduler_enabled():
        logger.info(
            '[learn-earn-stream] scheduler disabled '
            '(LEARN_EARN_PAYOUT_MODE not a streaming alias and '
            'LEARN_EARN_STREAM_SCHEDULER_ENABLED not forced on)'
        )
        return False
    try:
        get_stream_scheduler().start()
        return True
    except Exception as exc:  # noqa: BLE001
        logger.exception('[learn-earn-stream] failed to start: %s', exc)
        return False
