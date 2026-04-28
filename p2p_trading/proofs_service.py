"""
Payment-proof attachments for P2P trades, backed by Supabase Storage.

Each trade can have multiple uploaded proofs (e.g. screenshots of a GCash
receipt + bank confirmation). The actual files live in the private Storage
bucket ``payment-proofs``; this module owns:

* validating + uploading a binary file from a Flask route,
* recording the upload metadata in the ``p2p_trade_proofs`` table,
* generating short-lived signed URLs for buyer / seller / arbiter to view,
* listing proofs for a given trade.

Authentication is handled at the route layer (verify the wallet is the
buyer / seller / arbiter of the trade); this service does not re-check.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


DEFAULT_BUCKET = "payment-proofs"

ALLOWED_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
    "application/pdf",
}
ALLOWED_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "application/pdf": "pdf",
}

MAX_FILE_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_PROOFS_PER_TRADE = 10
SIGNED_URL_TTL_SECONDS = 60 * 60  # 1 hour


_TRADE_ID_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ProofValidationError(ValueError):
    """Raised when the uploaded file fails one of our validators."""


def _validate(
    *,
    trade_id: str,
    mime_type: str,
    size_bytes: int,
) -> None:
    if not _TRADE_ID_RE.match(trade_id or ""):
        raise ProofValidationError("Invalid trade_id format")
    if mime_type not in ALLOWED_MIME_TYPES:
        raise ProofValidationError(
            f"Unsupported file type {mime_type!r}; allowed: "
            + ", ".join(sorted(ALLOWED_MIME_TYPES))
        )
    if size_bytes <= 0:
        raise ProofValidationError("Empty file")
    if size_bytes > MAX_FILE_BYTES:
        raise ProofValidationError(
            f"File too large: {size_bytes} bytes (max {MAX_FILE_BYTES})"
        )


class P2PProofsService:
    """Upload / list / sign URLs for P2P payment-proof attachments."""

    bucket = DEFAULT_BUCKET

    def __init__(self, admin_client: Any = None, db_client: Any = None) -> None:
        self._admin_client = admin_client
        self._db_client = db_client

    # ---- lazy deps -------------------------------------------------------

    @property
    def admin(self) -> Any:
        """Service-role client (for Storage uploads / signed URLs)."""
        if self._admin_client is None:
            from supabase_client import get_supabase_admin_client

            self._admin_client = get_supabase_admin_client()
        if self._admin_client is None:
            raise RuntimeError(
                "Supabase admin client not configured. "
                "Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY."
            )
        return self._admin_client

    @property
    def db(self) -> Any:
        """Anon client used for ordinary DB reads/writes — same DB as admin
        but kept separate for parity with the rest of the codebase, which
        wires through ``supabase_client.get_supabase_client()``."""
        if self._db_client is None:
            from supabase_client import get_supabase_client

            self._db_client = get_supabase_client()
        if self._db_client is None:
            # Fall back to the admin client for DB ops; less ideal but better
            # than failing outright when only the service role is configured.
            return self.admin
        return self._db_client

    # ---- core operations -------------------------------------------------

    def upload(
        self,
        *,
        trade_id: str,
        uploader_wallet: str,
        file_bytes: bytes,
        mime_type: str,
        original_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Upload a single proof file to Storage + insert metadata row.

        Returns the inserted row (dict). Raises ``ProofValidationError`` on
        invalid input or ``RuntimeError`` if Supabase is misconfigured.
        """
        size_bytes = len(file_bytes)
        _validate(trade_id=trade_id, mime_type=mime_type, size_bytes=size_bytes)

        # Enforce per-trade limit before paying for an upload.
        existing = self.list_for_trade(trade_id, with_signed_urls=False)
        if len(existing) >= MAX_PROOFS_PER_TRADE:
            raise ProofValidationError(
                f"Trade already has the maximum of "
                f"{MAX_PROOFS_PER_TRADE} proofs"
            )

        ext = ALLOWED_EXTENSIONS[mime_type]
        # Path layout: <trade_id>/<uuid>.<ext>. Using the trade_id as a
        # folder makes it trivial to bulk-delete all proofs for a given
        # trade later (e.g. if a trade is fully completed and we want to
        # purge after a retention period).
        object_path = f"{trade_id}/{uuid.uuid4().hex}.{ext}"

        try:
            self.admin.storage.from_(self.bucket).upload(
                path=object_path,
                file=file_bytes,
                file_options={
                    "content-type": mime_type,
                    "upsert": "false",
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("Supabase storage upload failed")
            raise RuntimeError(f"Storage upload failed: {exc}") from exc

        row = {
            "trade_id": trade_id,
            "uploader_wallet": (uploader_wallet or "").lower(),
            "storage_bucket": self.bucket,
            "storage_path": object_path,
            "mime_type": mime_type,
            "size_bytes": size_bytes,
            "original_name": (original_name or "")[:255] or None,
            "created_at": _utcnow_iso(),
        }
        try:
            res = self.db.table("p2p_trade_proofs").insert(row).execute()
            inserted = (res.data or [None])[0] or row
        except Exception as exc:  # noqa: BLE001
            # Best-effort cleanup: remove the orphan file from Storage so we
            # don't leak space when the DB write fails.
            logger.exception("p2p_trade_proofs insert failed; cleaning Storage")
            try:
                self.admin.storage.from_(self.bucket).remove([object_path])
            except Exception:  # noqa: BLE001
                logger.warning("Failed to clean up orphan storage object %s", object_path)
            raise RuntimeError(f"DB insert failed: {exc}") from exc

        return inserted

    def list_for_trade(
        self,
        trade_id: str,
        *,
        with_signed_urls: bool = True,
    ) -> List[Dict[str, Any]]:
        """Return non-deleted proof rows for a trade, newest first."""
        if not _TRADE_ID_RE.match(trade_id or ""):
            return []
        try:
            res = (
                self.db.table("p2p_trade_proofs")
                .select("*")
                .eq("trade_id", trade_id)
                .is_("deleted_at", "null")
                .order("created_at", desc=True)
                .execute()
            )
            rows: List[Dict[str, Any]] = res.data or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("list_for_trade(%s) failed: %s", trade_id, exc)
            return []

        if with_signed_urls:
            for r in rows:
                r["signed_url"] = self._signed_url_safe(r.get("storage_path"))
        return rows

    def get_proof(self, proof_id: str) -> Optional[Dict[str, Any]]:
        """Fetch a single non-deleted proof row by id."""
        if not proof_id:
            return None
        try:
            res = (
                self.db.table("p2p_trade_proofs")
                .select("*")
                .eq("id", proof_id)
                .is_("deleted_at", "null")
                .limit(1)
                .execute()
            )
            data = res.data or []
            return data[0] if data else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("get_proof(%s) failed: %s", proof_id, exc)
            return None

    def signed_url(
        self,
        storage_path: str,
        ttl_seconds: int = SIGNED_URL_TTL_SECONDS,
    ) -> Optional[str]:
        """Generate a fresh signed URL for a stored object."""
        if not storage_path:
            return None
        try:
            res = self.admin.storage.from_(self.bucket).create_signed_url(
                storage_path, ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("create_signed_url(%s) failed: %s", storage_path, exc)
            return None
        # supabase-py returns either {"signedURL": "..."} or
        # {"signed_url": "..."} depending on version.
        if not isinstance(res, dict):
            return None
        return res.get("signedURL") or res.get("signed_url")

    def _signed_url_safe(self, storage_path: Optional[str]) -> Optional[str]:
        if not storage_path:
            return None
        try:
            return self.signed_url(storage_path)
        except Exception:  # noqa: BLE001
            return None


# Convenience helpers -------------------------------------------------------

def guess_mime_type(filename: str, fallback: str = "application/octet-stream") -> str:
    mime, _ = mimetypes.guess_type(filename or "")
    return mime or fallback


def split_filename(name: Optional[str]) -> Tuple[str, str]:
    """Return (basename, extension) for a user-supplied filename."""
    if not name:
        return ("", "")
    base = os.path.basename(name)
    if "." not in base:
        return (base, "")
    stem, ext = base.rsplit(".", 1)
    return (stem, ext.lower())


# Module-level singleton so callers can ``from .proofs_service import proofs_service``.
proofs_service = P2PProofsService()
