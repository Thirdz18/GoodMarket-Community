"""
In-trade chat between buyer / seller / arbiter, backed by Supabase.

Messages are stored as plaintext rows in the ``p2p_trade_chat`` table.
Auth is enforced at the route layer (only buyer / seller / arbiter of the
trade can read or write); this module is purely the data layer.

If admin-side privacy is required later, swap the plaintext ``body`` column
for client-side ECIES ciphertext — the rest of the API surface stays the
same.

Image attachments live in the private Storage bucket
``p2p-chat-attachments``; access is mediated by short-lived signed URLs
generated on every fetch.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_BUCKET = "p2p-chat-attachments"

ATTACHMENT_MIME_TYPES = {
    "image/png",
    "image/jpeg",
    "image/webp",
}
ATTACHMENT_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
}

MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024  # 5 MB
MAX_BODY_CHARS = 500
MAX_MESSAGES_PER_TRADE = 1000  # sanity cap to prevent unbounded growth
SIGNED_URL_TTL_SECONDS = 60 * 60  # 1 hour

# Off-chain DB trade_id format minted in escrow_service.create_trade(),
# e.g. ``TRADE-A1B2C3D4``. Keep in sync with proofs_service._TRADE_ID_RE.
_TRADE_ID_RE = re.compile(r"^TRADE-[0-9A-F]{8}$")

# Trade statuses that do NOT accept new messages. History is still readable.
_READ_ONLY_STATUSES = {"released", "cancelled", "refunded"}

VALID_ROLES = {"buyer", "seller", "arbiter"}


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatValidationError(ValueError):
    """Raised when a message fails one of our validators."""


def _validate_text(body: Optional[str]) -> Optional[str]:
    if body is None:
        return None
    body = body.strip()
    if not body:
        return None
    if len(body) > MAX_BODY_CHARS:
        raise ChatValidationError(
            f"Message too long: {len(body)} chars (max {MAX_BODY_CHARS})"
        )
    return body


def _validate_attachment(
    *, mime_type: Optional[str], size_bytes: int
) -> None:
    if mime_type not in ATTACHMENT_MIME_TYPES:
        raise ChatValidationError(
            f"Unsupported attachment type {mime_type!r}; allowed: "
            + ", ".join(sorted(ATTACHMENT_MIME_TYPES))
        )
    if size_bytes <= 0:
        raise ChatValidationError("Empty attachment")
    if size_bytes > MAX_ATTACHMENT_BYTES:
        raise ChatValidationError(
            f"Attachment too large: {size_bytes} bytes (max {MAX_ATTACHMENT_BYTES})"
        )


class P2PChatService:
    """Send / list / sign chat messages for a P2P trade."""

    bucket = DEFAULT_BUCKET

    def __init__(
        self,
        admin_client: Any = None,
        db_client: Any = None,
    ) -> None:
        self._admin_client = admin_client
        self._db_client = db_client

    # ---- lazy deps -------------------------------------------------------

    @property
    def admin(self) -> Any:
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
        if self._db_client is None:
            from supabase_client import get_supabase_client

            self._db_client = get_supabase_client()
        if self._db_client is None:
            return self.admin
        return self._db_client

    # ---- guards ----------------------------------------------------------

    @staticmethod
    def is_read_only(trade: Dict[str, Any]) -> bool:
        """Return True if no new messages should be accepted for this trade."""
        status = (trade.get("onchain_status") or "").lower()
        return status in _READ_ONLY_STATUSES

    # ---- core operations -------------------------------------------------

    def send(
        self,
        *,
        trade_id: str,
        sender_wallet: str,
        sender_role: str,
        body: Optional[str] = None,
        file_bytes: Optional[bytes] = None,
        mime_type: Optional[str] = None,
        original_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert a chat message; optionally upload an image attachment.

        Returns the inserted row. Raises ``ChatValidationError`` on bad input
        and ``RuntimeError`` if Supabase is misconfigured.
        """
        if not _TRADE_ID_RE.match(trade_id or ""):
            raise ChatValidationError("Invalid trade_id format")
        role = (sender_role or "").lower()
        if role not in VALID_ROLES:
            raise ChatValidationError(f"Invalid sender_role {role!r}")

        clean_body = _validate_text(body)
        has_attachment = file_bytes is not None and len(file_bytes) > 0
        if not clean_body and not has_attachment:
            raise ChatValidationError(
                "Message must include either text or an attachment"
            )

        existing_count = self._count_for_trade(trade_id)
        if existing_count >= MAX_MESSAGES_PER_TRADE:
            raise ChatValidationError(
                f"Trade has reached the per-trade message cap "
                f"({MAX_MESSAGES_PER_TRADE})"
            )

        attachment_path: Optional[str] = None
        attachment_size: Optional[int] = None
        if has_attachment:
            assert file_bytes is not None  # narrow for type-checker
            _validate_attachment(
                mime_type=mime_type, size_bytes=len(file_bytes)
            )
            ext = ATTACHMENT_EXTENSIONS[mime_type or ""]
            attachment_path = f"{trade_id}/{uuid.uuid4().hex}.{ext}"
            attachment_size = len(file_bytes)
            try:
                self.admin.storage.from_(self.bucket).upload(
                    path=attachment_path,
                    file=file_bytes,
                    file_options={
                        "content-type": mime_type,
                        "upsert": "false",
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("Chat attachment upload failed")
                raise RuntimeError(
                    f"Attachment upload failed: {exc}"
                ) from exc

        row: Dict[str, Any] = {
            "trade_id": trade_id,
            "sender_wallet": (sender_wallet or "").lower(),
            "sender_role": role,
            "body": clean_body,
            "attachment_bucket": self.bucket if has_attachment else None,
            "attachment_path": attachment_path,
            "attachment_mime": mime_type if has_attachment else None,
            "attachment_size": attachment_size,
            "created_at": _utcnow_iso(),
        }
        # Use admin client to bypass RLS for INSERT operations
        db = self.admin or self.db
        try:
            res = db.table("p2p_trade_chat").insert(row).execute()
            inserted = (res.data or [None])[0] or row
        except Exception as exc:  # noqa: BLE001
            # Best-effort orphan cleanup so a DB hiccup doesn't leak Storage.
            if attachment_path:
                try:
                    self.admin.storage.from_(self.bucket).remove(
                        [attachment_path]
                    )
                except Exception:  # noqa: BLE001
                    logger.warning(
                        "Failed to clean orphan chat attachment %s",
                        attachment_path,
                    )
            logger.exception("p2p_trade_chat insert failed")
            raise RuntimeError(f"DB insert failed: {exc}") from exc

        return inserted

    def list_for_trade(
        self,
        trade_id: str,
        *,
        since_iso: Optional[str] = None,
        limit: int = 200,
    ) -> List[Dict[str, Any]]:
        """Return non-deleted messages for a trade in ascending order.

        ``since_iso``: if provided, only messages with ``created_at >`` this
        timestamp are returned (cursor for polling).
        """
        if not _TRADE_ID_RE.match(trade_id or ""):
            return []
        try:
            q = (
                self.db.table("p2p_trade_chat")
                .select("*")
                .eq("trade_id", trade_id)
                .is_("deleted_at", "null")
                .order("created_at", desc=False)
                .limit(max(1, min(int(limit), MAX_MESSAGES_PER_TRADE)))
            )
            if since_iso:
                q = q.gt("created_at", since_iso)
            res = q.execute()
            return res.data or []
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "chat list_for_trade(%s) failed: %s", trade_id, exc
            )
            return []

    def get_message(self, message_id: str) -> Optional[Dict[str, Any]]:
        if not message_id:
            return None
        try:
            res = (
                self.db.table("p2p_trade_chat")
                .select("*")
                .eq("id", message_id)
                .is_("deleted_at", "null")
                .limit(1)
                .execute()
            )
            data = res.data or []
            return data[0] if data else None
        except Exception as exc:  # noqa: BLE001
            logger.warning("chat get_message(%s) failed: %s", message_id, exc)
            return None

    def signed_url(
        self,
        storage_path: str,
        ttl_seconds: int = SIGNED_URL_TTL_SECONDS,
    ) -> Optional[str]:
        if not storage_path:
            return None
        try:
            res = self.admin.storage.from_(self.bucket).create_signed_url(
                storage_path, ttl_seconds
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "chat create_signed_url(%s) failed: %s", storage_path, exc
            )
            return None
        if not isinstance(res, dict):
            return None
        return res.get("signedURL") or res.get("signed_url")

    def latest_for_sender(
        self, trade_id: str, sender_wallet: str
    ) -> Optional[Dict[str, Any]]:
        """Return the most recent message a wallet sent in a trade, or None.

        Used by the route layer to enforce a simple per-sender rate limit.
        """
        if not _TRADE_ID_RE.match(trade_id or ""):
            return None
        try:
            res = (
                self.db.table("p2p_trade_chat")
                .select("created_at")
                .eq("trade_id", trade_id)
                .eq("sender_wallet", (sender_wallet or "").lower())
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            data = res.data or []
            return data[0] if data else None
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "chat latest_for_sender(%s, %s) failed: %s",
                trade_id, sender_wallet, exc,
            )
            return None

    def _count_for_trade(self, trade_id: str) -> int:
        try:
            res = (
                self.db.table("p2p_trade_chat")
                .select("id", count="exact")
                .eq("trade_id", trade_id)
                .is_("deleted_at", "null")
                .limit(1)
                .execute()
            )
            return int(getattr(res, "count", 0) or 0)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "chat _count_for_trade(%s) failed: %s", trade_id, exc
            )
            return 0


# Module-level singleton.
chat_service = P2PChatService()
