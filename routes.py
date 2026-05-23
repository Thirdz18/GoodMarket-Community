from flask import Blueprint, render_template, request, jsonify, session, redirect, url_for, make_response
from blockchain import has_recent_ubi_claim, GOODDOLLAR_CONTRACTS
from analytics_service import analytics
from supabase_client import get_supabase_client, get_supabase_admin_client, safe_supabase_operation, supabase_logger, log_admin_action
from notifications_service import NotificationService
from web3 import Web3
import json
import logging
import os
import threading
import time
import urllib.request
import urllib.error
import uuid
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from datetime import datetime, timedelta, timezone
import re
from collaboration_automation import (
    automate_collaboration_assets,
    generate_collaboration_quiz_draft as generate_collaboration_quiz_draft_rows
)

# Initialize notification service
notification_service = NotificationService()

# Logger for this module
logger = logging.getLogger(__name__)

# Simple TTL caches for high-frequency public endpoints
_price_visibility_cache: dict = {"data": None, "expires": 0}
_feature_visibility_cache: dict = {"data": None, "expires": 0}
_PUBLIC_ENDPOINT_CACHE_TTL = 60  # seconds

# Create Blueprint FIRST - BEFORE any route decorators
routes = Blueprint("routes", __name__)

def auth_required(f):
    """Decorator for endpoints requiring authentication with auto-logout on expiry"""
    def wrapper(*args, **kwargs):
        wallet = session.get("wallet")
        verified = session.get("verified")

        if not verified or not wallet:
            return jsonify({"success": False, "error": "Authentication required"}), 401

        # UBI check temporarily disabled — all verified sessions allowed
        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper

def admin_required(f):
    """Decorator for endpoints requiring admin authentication"""
    def wrapper(*args, **kwargs):
        wallet = session.get("wallet")
        if not session.get("verified") or not wallet:
            return jsonify({"success": False, "error": "Authentication required"}), 401

        from supabase_client import is_admin
        if not is_admin(wallet):
            return jsonify({"success": False, "error": "Admin access required"}), 403

        return f(*args, **kwargs)
    wrapper.__name__ = f.__name__
    return wrapper


def _parse_iso_datetime(value):
    """Safely parse ISO datetime strings to timezone-aware UTC datetime."""
    if not value:
        return None
    try:
        normalized = value.replace("Z", "+00:00") if isinstance(value, str) else value
        dt = datetime.fromisoformat(normalized) if isinstance(normalized, str) else None
        if not dt:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _coerce_bool(value) -> bool:
    """Safely coerce mixed input values to bool without treating 'false' as truthy."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off", ""}:
            return False
    if isinstance(value, (int, float)):
        return value != 0
    return False


@routes.route("/api/claims/v2/confirm", methods=["POST"])
@auth_required
def confirm_goodmarket_claim():
    """Record GoodMarket-attributed claim transaction in Supabase facts/events tables.

    Writes go through the service-role client (``get_supabase_admin_client``)
    because the wallet user is *not* a Supabase Auth user and the tables have
    RLS enabled with no anon-key policy. The anon client is kept as a
    last-resort fallback for local/dev setups that haven't configured a
    service-role key yet.
    """
    payload = request.get_json(silent=True) or {}
    wallet = (session.get("wallet") or "").strip().lower()
    if not wallet:
        return jsonify({"success": False, "error": "Authentication required"}), 401

    tx_hash = str(payload.get("tx_hash") or "").strip().lower()
    network = str(payload.get("network") or "celo").strip().lower()
    status = str(payload.get("status") or "confirmed").strip().lower()
    correlation_id = str(payload.get("correlation_id") or "").strip() or None

    # claim_attempt_id is stored as Postgres `uuid`. The frontend may fall
    # back to a non-UUID string (e.g. "attempt-…") on older in-app wallet
    # browsers without crypto.randomUUID. Validate strictly and regenerate
    # if invalid so the insert doesn't blow up on type cast.
    raw_attempt_id = str(payload.get("claim_attempt_id") or "").strip()
    try:
        claim_attempt_id = str(uuid.UUID(raw_attempt_id)) if raw_attempt_id else str(uuid.uuid4())
    except (ValueError, TypeError):
        logger.info(
            f"[gm-claim-confirm] invalid claim_attempt_id from client "
            f"({raw_attempt_id!r}) — regenerating server-side"
        )
        claim_attempt_id = str(uuid.uuid4())

    if not tx_hash.startswith("0x") or len(tx_hash) < 10:
        return jsonify({"success": False, "error": "Invalid tx_hash"}), 400
    if network not in ("celo", "xdc"):
        return jsonify({"success": False, "error": "Invalid network"}), 400
    if status not in ("submitted", "confirmed", "failed", "rejected", "unknown"):
        return jsonify({"success": False, "error": "Invalid status"}), 400

    # Prefer the service-role client so RLS doesn't silently drop writes.
    sb = get_supabase_admin_client()
    client_kind = "service_role"
    if sb is None:
        sb = get_supabase_client()
        client_kind = "anon_fallback"
        logger.warning(
            "[gm-claim-confirm] SUPABASE_SERVICE_ROLE_KEY not set — falling back to anon client. "
            "Inserts will fail if RLS is enabled on goodmarket_claim_facts/_events."
        )
    if sb is None:
        logger.error("[gm-claim-confirm] No Supabase client available")
        return jsonify({"success": False, "error": "Storage unavailable"}), 503

    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        existing_resp = sb.table("goodmarket_claim_facts")\
            .select("id, status, submitted_at, confirmed_at")\
            .eq("tx_hash", tx_hash)\
            .limit(1)\
            .execute()
        existing = existing_resp.data[0] if existing_resp.data else None

        row = {
            "wallet_address": wallet,
            "network": network,
            "tx_hash": tx_hash,
            "source": "goodmarket_wallet_ui",
            "claim_attempt_id": claim_attempt_id,
            "correlation_id": correlation_id,
            "status": status,
            "verification_state": "pending" if status == "submitted" else "verified",
            "updated_at": now_iso,
        }
        if status in ("submitted", "confirmed"):
            row["submitted_at"] = existing.get("submitted_at") if existing and existing.get("submitted_at") else now_iso
        if status == "confirmed":
            row["confirmed_at"] = now_iso

        if existing:
            sb.table("goodmarket_claim_facts").update(row).eq("id", existing["id"]).execute()
            logger.info(
                f"[gm-claim-confirm] facts UPDATE wallet={wallet} network={network} "
                f"tx={tx_hash[:12]}… status={status} client={client_kind}"
            )
        else:
            row["created_at"] = now_iso
            sb.table("goodmarket_claim_facts").insert(row).execute()
            logger.info(
                f"[gm-claim-confirm] facts INSERT wallet={wallet} network={network} "
                f"tx={tx_hash[:12]}… status={status} client={client_kind}"
            )
    except Exception as e_facts:
        # Most common cause in production is "relation does not exist" when the
        # SQL migration in sql/goodmarket_claim_attribution_v2.sql hasn't been
        # applied yet. Surface it clearly instead of silently 500-ing.
        msg = str(e_facts)
        is_missing_table = "does not exist" in msg.lower() or "relation" in msg.lower()
        logger.error(
            f"[gm-claim-confirm] facts upsert failed (client={client_kind}, "
            f"missing_table={is_missing_table}): {msg}"
        )
        return jsonify({
            "success": False,
            "error": (
                "GoodMarket claim attribution tables not provisioned. "
                "Run sql/goodmarket_claim_attribution_v2.sql in the Supabase SQL editor."
            ) if is_missing_table else "Could not record claim",
            "diagnostic": msg[:240],
        }), 500

    # Best-effort event log — never block the claim record on this.
    try:
        event_type = "claim_tx_confirmed" if status == "confirmed" else "claim_tx_submitted"
        if status == "failed":
            event_type = "claim_tx_failed"
        elif status == "rejected":
            event_type = "claim_tx_rejected"
        sb.table("goodmarket_claim_events").insert({
            "claim_attempt_id": claim_attempt_id,
            "wallet_address": wallet,
            "network": network,
            "tx_hash": tx_hash,
            "event_type": event_type,
            "source": "goodmarket_wallet_ui",
            "correlation_id": correlation_id,
            "created_at": now_iso,
        }).execute()
    except Exception as e_evt:
        # Idempotency unique-index violations are expected on repeat calls.
        logger.warning(f"[gm-claim-confirm] event insert skipped: {e_evt}")

    # Best-effort attribution backfill: any wallet recording a claim through
    # the GoodMarket UI is, by definition, "using GoodMarket". If they're
    # also face-verified on-chain (verified inside the helper) but their
    # user_data row still has verified_after_goodmarket=False, flip it now.
    # Runs on a daemon thread so we don't add latency to the claim response.
    try:
        from goodmarket_attribution_backfill import mark_verified_via_goodmarket
        mark_verified_via_goodmarket(
            wallet,
            source=f"claim_confirm:{network}:{status}",
            background=True,
        )
    except Exception as e_attr:
        logger.warning(f"[gm-claim-confirm] attribution backfill skipped: {e_attr}")

    return jsonify({
        "success": True,
        "tx_hash": tx_hash,
        "status": status,
        "claim_attempt_id": claim_attempt_id,
    })


@routes.route("/api/claims/v2/health", methods=["GET"])
@auth_required
def goodmarket_claim_health():
    """Quick diagnostic for the GoodMarket claim attribution stack.

    Returns whether the service-role client is wired up and whether the
    facts/events tables are reachable. Use this from the browser console
    (or curl with a session cookie) when claims look like they aren't
    being recorded:

        await fetch('/api/claims/v2/health').then(r => r.json())
    """
    admin_sb = get_supabase_admin_client()
    anon_sb = get_supabase_client()
    out = {
        "service_role_configured": admin_sb is not None,
        "anon_client_configured": anon_sb is not None,
        "facts_table": {"reachable": False, "error": None, "client": None},
        "events_table": {"reachable": False, "error": None, "client": None},
    }

    sb = admin_sb or anon_sb
    client_kind = "service_role" if admin_sb is not None else ("anon" if anon_sb is not None else None)
    if sb is None:
        return jsonify(out), 200

    for table_key, table_name in (("facts_table", "goodmarket_claim_facts"),
                                  ("events_table", "goodmarket_claim_events")):
        try:
            sb.table(table_name).select("id", count="exact").limit(1).execute()
            out[table_key]["reachable"] = True
            out[table_key]["client"] = client_kind
        except Exception as e:
            msg = str(e)
            out[table_key]["error"] = msg[:240]
            out[table_key]["client"] = client_kind
            if "does not exist" in msg.lower() or "relation" in msg.lower():
                out[table_key]["hint"] = (
                    "Run sql/goodmarket_claim_attribution_v2.sql in the Supabase SQL editor."
                )
            elif "row-level security" in msg.lower() or "rls" in msg.lower():
                out[table_key]["hint"] = (
                    "Set SUPABASE_SERVICE_ROLE_KEY in the Vercel project so writes can bypass RLS."
                )

    return jsonify(out), 200


@routes.route("/api/claims/v2/reconcile-one", methods=["POST"])
@admin_required
def goodmarket_claim_reconcile_one():
    """Admin one-shot recheck of a single GoodMarket claim tx.

    Body: ``{"tx_hash": "0x…", "network": "celo"|"xdc"}``.

    Performs the same receipt fetch + DB update as the periodic reconciler
    for a single row. Idempotent — calling it on an already-confirmed row
    returns ``{"no_change": true, "status": "confirmed"}`` and does not
    duplicate ``goodmarket_claim_events``.

    Useful for rescuing rows that got stuck at ``status='submitted'``
    because the wallet UI never delivered the receipt callback.
    """
    payload = request.get_json(silent=True) or {}
    tx_hash = str(payload.get("tx_hash") or "").strip().lower()
    network = str(payload.get("network") or "").strip().lower()

    if not tx_hash.startswith("0x") or len(tx_hash) < 10:
        return jsonify({"success": False, "error": "Invalid tx_hash"}), 400
    if network not in ("celo", "xdc"):
        return jsonify({"success": False, "error": "Invalid network"}), 400

    try:
        from goodmarket_claim_reconciler import reconcile_one
        result = reconcile_one(tx_hash, network)
    except Exception as exc:
        logger.error(f"[gm-reconcile-one] crashed tx={tx_hash}: {exc}")
        return jsonify({"success": False, "error": str(exc)[:240]}), 500

    admin_wallet = (session.get("wallet") or "").lower()
    logger.info(
        f"[gm-reconcile-one] admin={admin_wallet[:10]}… tx={tx_hash[:12]}… "
        f"network={network} result={result}"
    )

    status_code = 200 if result.get("success") else 400
    return jsonify(result), status_code


@routes.route("/api/claims/v2/reconciler-status", methods=["GET"])
@admin_required
def goodmarket_claim_reconciler_status():
    """Diagnostic snapshot of the GoodMarket claim reconciler worker.

    Returns whether the background loop is running, its config, and the
    last cycle's summary. Use this to sanity-check that the reconciler
    is actually polling in production:

        await fetch('/api/claims/v2/reconciler-status').then(r => r.json())
    """
    try:
        from goodmarket_claim_reconciler import get_reconciler
        return jsonify({"success": True, **get_reconciler().status()}), 200
    except Exception as exc:
        logger.error(f"[gm-reconciler-status] error: {exc}")
        return jsonify({"success": False, "error": str(exc)[:240]}), 500


def _parse_xdc_revert_message(raw_message: str, fallback_reason: str = "Transaction reverted on XDC.") -> dict:
    """Normalize XDC revert outputs into user-facing and technical fields."""
    raw_msg = str(raw_message or "").strip()
    parsed = ""
    lower_msg = raw_msg.lower()

    marker_idx = lower_msg.find("execution reverted:")
    if marker_idx == -1:
        marker_idx = lower_msg.find("revert")
    if marker_idx != -1:
        parsed = raw_msg[marker_idx:].split("\n")[0].strip()
        if parsed.lower().startswith("execution reverted:"):
            parsed = parsed.split(":", 1)[1].strip()
        elif parsed.lower().startswith("revert"):
            parsed = parsed[6:].strip(" :")
    elif raw_msg:
        parsed = raw_msg[:240]

    selector_match = re.search(r"0x[a-fA-F0-9]{8}", parsed or raw_msg)
    tuple_like = bool(re.search(r"\(\s*['\"]?0x[a-fA-F0-9]{8}['\"]?\s*,\s*['\"]?0x[a-fA-F0-9]{8}['\"]?\s*\)", parsed or raw_msg))
    selector_only = bool(parsed and re.fullmatch(r"['\"]?0x[a-fA-F0-9]{8}['\"]?", parsed))

    selector_catalog = {
        # GoodDollar MessagePassingBridge known selectors (observed in production)
        "0x10ecdf44": {
            "label": "LayerZero/bridge fee check failed",
            "user_reason": (
                "Bridge fee is no longer sufficient for this route right now. "
                "Refresh fee estimate and retry with a slightly higher XDC fee."
            ),
            "category": "fee_mismatch",
        },
        "0xc5426f8d": {
            "label": "Bridge route is paused/closed",
            "user_reason": "Bridge route is currently paused. Please retry later.",
            "category": "route_paused",
        },
        "0x92a27eac": {
            "label": "LayerZero fee mismatch",
            "user_reason": (
                "Bridge fee sent is lower than required right now. "
                "Refresh fee estimate and retry with a slightly higher fee."
            ),
            "category": "fee_mismatch",
        },
        "0x2e9394cc": {
            "label": "Missing bridge fee",
            "user_reason": "Missing bridge fee. Enter a positive XDC bridge fee and retry.",
            "category": "fee_mismatch",
        },
        "0x068a5053": {
            "label": "Unsupported target chain",
            "user_reason": "Selected target chain is not supported by the bridge route.",
            "category": "route_config",
        },
        "0x2c863d26": {
            "label": "Token transferFrom failed",
            "user_reason": "Token transfer could not be completed. Re-check balance and allowance.",
            "category": "token_constraints",
        },
        "0x76420e1d": {
            "label": "Token transfer failed",
            "user_reason": "Token transfer failed on bridge contract. Re-check token balance and route health.",
            "category": "token_constraints",
        },
    }

    technical_details = None
    user_reason = fallback_reason
    selector = selector_match.group(0).lower() if selector_match else None
    selector_meta = selector_catalog.get(selector) if selector else None
    if selector_match and (tuple_like or selector_only):
        user_reason = (
            selector_meta.get("user_reason")
            if selector_meta
            else "Bridge could not process this transfer. Please confirm bridge amount and token pair (XDC G$ → Celo G$), then try again."
        )
        technical_details = (
            f"Bridge custom error ({selector}: {selector_meta.get('label')})"
            if selector_meta
            else f"Bridge custom error ({selector})"
        )
    elif parsed:
        cleaned = parsed.strip()
        if cleaned:
            user_reason = cleaned
        if selector:
            technical_details = (
                f"Bridge custom error ({selector}: {selector_meta.get('label')})"
                if selector_meta
                else f"Bridge custom error ({selector})"
            )
    else:
        if selector:
            technical_details = (
                f"Bridge custom error ({selector}: {selector_meta.get('label')})"
                if selector_meta
                else f"Bridge custom error ({selector})"
            )

    return {
        "reason": user_reason or fallback_reason,
        "technical_details": technical_details,
        "error_selector": selector,
        "error_category": selector_meta.get("category") if selector_meta else None,
        "raw_reason": parsed or raw_msg or None,
    }


def _parse_bridge_fee_candidate_xdc(candidate) -> Decimal | None:
    """Parse bridge fee candidate into a native-token decimal amount.

    GoodDollar's https://goodserver.gooddollar.org/bridge/estimatefees returns
    values shaped like ``"0.11559585178717026 Celo"`` / ``"1.79987 XDC"`` /
    ``"0.00038 ETH"`` / ``"3.32 Fuse"`` — i.e. a decimal followed by a space
    and the source-chain native-token name. Strip that trailing suffix before
    parsing so docs-aligned route values like ``LZ_CELO_TO_XDC`` are accepted.
    """
    if candidate is None:
        return None
    try:
        if isinstance(candidate, bool):
            return None

        if isinstance(candidate, (int, float)):
            numeric = Decimal(str(candidate))
        elif isinstance(candidate, str):
            txt = candidate.strip()
            if not txt:
                return None
            # Strip trailing native-token suffix used by goodserver/estimatefees
            # (e.g. "0.11559585178717026 Celo" → "0.11559585178717026").
            for suffix in (
                " celo", " xdc", " eth", " ether", " fuse",
            ):
                if txt.lower().endswith(suffix):
                    txt = txt[: -len(suffix)].strip()
                    break
            if not txt:
                return None
            if txt.startswith(("0x", "0X")):
                numeric = Decimal(int(txt, 16))
            else:
                numeric = Decimal(txt)
        else:
            return None

        if numeric <= 0:
            return None

        # Some APIs return wei (e.g., "1617600000000000000") instead of decimal
        # native units. Detect very large integer-like numbers and normalize.
        if numeric >= Decimal("1e9"):
            return numeric / Decimal("1e18")
        return numeric
    except (InvalidOperation, ValueError, TypeError):
        return None


def _extract_xdc_revert_reason(w3, call_obj: dict, replay_block: int, fallback_reason: str = "Transaction reverted on XDC.") -> dict:
    """Replay a failed XDC call and normalize reason output."""
    try:
        w3.eth.call(call_obj, replay_block)
        return {
            "reason": fallback_reason,
            "technical_details": None,
            "error_selector": None,
            "raw_reason": None,
        }
    except Exception as call_err:
        return _parse_xdc_revert_message(str(call_err or ""), fallback_reason=fallback_reason)


def _decode_bridge_to_input(input_data: str) -> dict | None:
    """Best-effort decoder for bridgeTo(address,uint256,uint256,uint8) calldata."""
    try:
        data = (input_data or "").strip().lower()
        if not data.startswith("0x1fec5c5c"):
            return None
        payload = data[10:]
        if len(payload) < (32 * 4 * 2):
            return None

        chunks = [payload[i:i + 64] for i in range(0, 64 * 4, 64)]
        target = "0x" + chunks[0][-40:]
        target_chain_id = int(chunks[1], 16)
        amount_wei = int(chunks[2], 16)
        bridge_service = int(chunks[3], 16)
        return {
            "method_id": "0x1fec5c5c",
            "target": target,
            "target_chain_id": target_chain_id,
            "amount_wei": str(amount_wei),
            "amount_gd": str(amount_wei / (10 ** 18)),
            "bridge_service": bridge_service,
        }
    except Exception:
        return None

@routes.route('/api/daily-task/claim', methods=['POST'])
@auth_required
def claim_daily_task():
    """Claim unified daily task (Twitter or Telegram)"""
    try:
        wallet = session.get('wallet')
        data = request.get_json()

        platform = data.get('platform')  # 'twitter' or 'telegram'
        post_url = data.get('post_url')

        if platform not in ['twitter', 'telegram']:
            return jsonify({
                'success': False,
                'error': 'Invalid platform. Choose twitter or telegram.'
            }), 400

        if not post_url:
            return jsonify({
                'success': False,
                'error': f'{platform.capitalize()} post URL is required'
            }), 400

        # Import appropriate service
        if platform == 'twitter':
            from twitter_task.twitter_task import twitter_task_service
            service = twitter_task_service
        else:  # telegram
            from telegram_task.telegram_task import telegram_task_service
            service = telegram_task_service

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = loop.run_until_complete(
                service.claim_task_reward(wallet, post_url)
            )

            if result is None:
                logger.error(f"❌ claim_task_reward returned None for platform={platform}")
                return jsonify({'success': False, 'error': 'Unexpected error processing your submission. Please try again.'}), 500

            if result.get('success'):
                return jsonify(result), 200
            else:
                return jsonify(result), 400

        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Daily task claim error: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'success': False, 'error': 'Failed to claim reward'}), 500

@routes.route('/api/daily-task/status', methods=['GET'])
@auth_required
def get_daily_task_status():
    """Get unified daily task status (checks both Twitter and Telegram)"""
    try:
        wallet = session.get('wallet')

        # Import both services
        from twitter_task.twitter_task import twitter_task_service
        from telegram_task.telegram_task import telegram_task_service
        from datetime import datetime, timezone

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            # Check both tasks
            twitter_status = loop.run_until_complete(twitter_task_service.check_eligibility(wallet))
            telegram_status = loop.run_until_complete(telegram_task_service.check_eligibility(wallet))

            # CRITICAL FIX: Check platforms for pending AND check database for actual pending submissions
            # This ensures real-time accuracy even with caching issues

            # First, check direct database for ANY pending submissions
            supabase = get_supabase_client()
            actual_pending = False
            actual_pending_platform = None

            if supabase:
                # Check Twitter pending
                twitter_pending_check = safe_supabase_operation(
                    lambda: supabase.table('twitter_task_log')\
                        .select('id')\
                        .eq('wallet_address', wallet)\
                        .eq('status', 'pending')\
                        .limit(1)\
                        .execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="check twitter pending"
                )

                if twitter_pending_check.data and len(twitter_pending_check.data) > 0:
                    actual_pending = True
                    actual_pending_platform = 'Twitter'

                # Check Telegram pending only if Twitter not pending
                if not actual_pending:
                    telegram_pending_check = safe_supabase_operation(
                        lambda: supabase.table('telegram_task_log')\
                            .select('id')\
                            .eq('wallet_address', wallet)\
                            .eq('status', 'pending')\
                            .limit(1)\
                            .execute(),
                        fallback_result=type('obj', (object,), {'data': []})(),
                        operation_name="check telegram pending"
                    )

                    if telegram_pending_check.data and len(telegram_pending_check.data) > 0:
                        actual_pending = True
                        actual_pending_platform = 'Telegram'

            # Determine pending platform based on actual database check
            if actual_pending:
                pending_platform = actual_pending_platform
            else:
                pending_platform = None

            # Determine next claim time based on eligible platform cooldowns
            next_claim_time = None
            if actual_pending:
                # If there's a pending submission, next_claim_time is not relevant for claiming
                pass
            else:
                # Check for cooldown (completed claims) - if ANY platform has cooldown, ALL are blocked
                if not twitter_status.get('can_claim') or not telegram_status.get('can_claim'):
                    # If any platform has cooldown active (from completed claims), all are blocked
                    twitter_next = twitter_status.get('next_claim_time')
                    telegram_next = telegram_status.get('next_claim_time')

                    # Find the earliest next claim time among all platforms
                    possible_next_claims = [t for t in [twitter_next, telegram_next] if t]
                    if possible_next_claims:
                        next_claim_time = min(possible_next_claims)

            # Calculate time remaining if next_claim_time exists
            time_remaining_seconds = 0
            if next_claim_time:
                next_claim_dt = datetime.fromisoformat(next_claim_time.replace('Z', '+00:00'))
                now = datetime.now(timezone.utc)
                time_remaining_seconds = max(0, int((next_claim_dt - now).total_seconds()))

            # User can claim if ALL platforms are available (shared cooldown) and no pending submissions
            can_claim = twitter_status.get('can_claim', False) and \
                        telegram_status.get('can_claim', False) and \
                        not actual_pending

            return jsonify({
                'can_claim': can_claim,
                'has_pending_submission': actual_pending,
                'pending_platform': pending_platform,
                'next_claim_time': next_claim_time,
                'time_remaining_seconds': time_remaining_seconds
            }), 200
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Daily task status error: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({'error': 'Failed to get task status'}), 500


@routes.route('/api/daily-task/history', methods=['GET'])
@auth_required
def get_daily_task_history():
    """Get combined Twitter and Telegram task history"""
    try:
        wallet = session.get('wallet')
        limit = int(request.args.get('limit', 50))

        from twitter_task.twitter_task import twitter_task_service
        from telegram_task.telegram_task import telegram_task_service

        # Get all histories
        twitter_history = twitter_task_service.get_transaction_history(wallet, limit)
        telegram_history = telegram_task_service.get_transaction_history(wallet, limit)

        # Combine transactions
        all_transactions = []

        if twitter_history.get('success') and twitter_history.get('transactions'):
            for tx in twitter_history['transactions']:
                tx['platform'] = 'twitter'
                # Ensure rejection_reason is included
                if 'rejection_reason' not in tx:
                    tx['rejection_reason'] = None
                all_transactions.append(tx)

        if telegram_history.get('success') and telegram_history.get('transactions'):
            for tx in telegram_history['transactions']:
                tx['platform'] = 'telegram'
                # Ensure rejection_reason is included
                if 'rejection_reason' not in tx:
                    tx['rejection_reason'] = None
                all_transactions.append(tx)

        # Sort by date (newest first)
        all_transactions.sort(key=lambda x: x.get('created_at', ''), reverse=True)

        # Limit results
        all_transactions = all_transactions[:limit]

        # Calculate totals
        total_earned = sum(float(tx.get('reward_amount', 0)) for tx in all_transactions)

        return jsonify({
            'success': True,
            'transactions': all_transactions,
            'total_count': len(all_transactions),
            'total_earned': total_earned
        })

    except Exception as e:
        logger.error(f"❌ Daily task history error: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({
            'success': False,
            'error': 'Failed to get history',
            'transactions': [],
            'total_count': 0,
            'total_earned': 0
        }), 500


@routes.route("/api/recent-daily-tasks", methods=["GET"])
def get_recent_daily_tasks():
    """Get recent daily task submissions from last 72 hours"""
    try:
        from datetime import datetime, timedelta
        from supabase_client import get_supabase_client
        from cache_utils import api_cache, cached

        # Check cache first (2 minute TTL)
        cache_key = "recent_daily_tasks"
        cached_result = api_cache.get(cache_key)
        if cached_result:
            response = jsonify(cached_result)
            response.headers['Content-Type'] = 'application/json'
            response.headers['Cache-Control'] = 'public, max-age=120'
            return response, 200

        supabase = get_supabase_client()
        if not supabase:
            response = jsonify({"success": False, "submissions": []})
            response.headers['Content-Type'] = 'application/json'
            return response, 200

        # Calculate 72 hours ago (aligned with daily task cooldown window)
        cooldown_window_start = (datetime.utcnow() - timedelta(hours=72)).isoformat()

        # Get Twitter task submissions from last 72 hours
        twitter_submissions = safe_supabase_operation(
            lambda: supabase.table('twitter_task_log')\
                .select('wallet_address, reward_amount, created_at, twitter_url')\
                .gte('created_at', cooldown_window_start)\
                .order('created_at', desc=True)\
                .limit(50)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get recent twitter tasks"
        )

        # Get Telegram task submissions from last 72 hours
        telegram_submissions = safe_supabase_operation(
            lambda: supabase.table('telegram_task_log')\
                .select('wallet_address, reward_amount, created_at, telegram_url')\
                .gte('created_at', cooldown_window_start)\
                .order('created_at', desc=True)\
                .limit(50)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get recent telegram tasks"
        )

        # Combine and format submissions WITH MESSAGES/LINKS
        all_submissions = []

        # Add Twitter submissions WITH LINKS - USE CACHED USERNAMES
        if twitter_submissions and twitter_submissions.data:
            for sub in twitter_submissions.data:
                wallet = sub.get('wallet_address', '')

                all_submissions.append({
                    'wallet_address': wallet,
                    'display_name': f"{wallet[:6]}...{wallet[-4:]}",
                    'reward_amount': float(sub.get('reward_amount', 0)),
                    'created_at': sub.get('created_at'),
                    'platform': 'Twitter',
                    'submission_url': sub.get('twitter_url', ''),
                    'submission_type': 'twitter_post',
                    'status': sub.get('status', 'completed'),
                    'rejection_reason': sub.get('rejection_reason')
                })

        # Add Telegram submissions WITH LINKS
        if telegram_submissions and telegram_submissions.data:
            for sub in telegram_submissions.data:
                wallet = sub.get('wallet_address', '')

                all_submissions.append({
                    'wallet_address': wallet,
                    'display_name': f"{wallet[:6]}...{wallet[-4:]}",
                    'reward_amount': float(sub.get('reward_amount', 0)),
                    'created_at': sub.get('created_at'),
                    'platform': 'Telegram',
                    'submission_url': sub.get('telegram_url', ''),
                    'submission_type': 'telegram_post',
                    'status': sub.get('status', 'completed'),
                    'rejection_reason': sub.get('rejection_reason')
                })

        # Sort by created_at (newest first)
        all_submissions.sort(key=lambda x: x['created_at'], reverse=True)

        # Limit to 20 most recent
        all_submissions = all_submissions[:20]

        logger.info(f"✅ Returning {len(all_submissions)} recent daily task submissions")

        result = {
            "success": True,
            "submissions": all_submissions,
            "total_count": len(all_submissions)
        }

        # Cache for 2 minutes for better performance
        api_cache.set(cache_key, result, ttl=120)

        response = jsonify(result)
        response.headers['Content-Type'] = 'application/json'
        response.headers['Cache-Control'] = 'public, max-age=120'
        return response, 200

    except Exception as e:
        logger.error(f"❌ Error getting recent daily tasks: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        error_response = jsonify({"success": False, "submissions": [], "error": str(e)})
        error_response.headers['Content-Type'] = 'application/json'
        return error_response, 500

@routes.route("/api/learn-earn-participants", methods=["GET"])
def get_learn_earn_participants():
    """Get Learn & Earn participants for a specific date or date range"""
    try:
        from datetime import datetime
        from supabase_client import get_supabase_client

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "participants": []})

        # Get date parameter (format: YYYY-MM-DD)
        target_date = request.args.get('date')

        if target_date:
            # Query for specific date with proper UTC timezone format
            start_datetime = f"{target_date}T00:00:00Z"
            end_datetime = f"{target_date}T23:59:59Z"
        else:
            # Default to today with proper UTC timezone format
            today = datetime.utcnow().strftime('%Y-%m-%d')
            start_datetime = f"{today}T00:00:00Z"
            end_datetime = f"{today}T23:59:59Z"

        logger.info(f"📊 Fetching Learn & Earn participants for {target_date or 'today'}")
        logger.info(f"🕐 Date range: {start_datetime} to {end_datetime}")

        # Get all Learn & Earn participants for the date
        participants = safe_supabase_operation(
            lambda: supabase.table('learnearn_log')\
                .select('wallet_address, amount_g$, timestamp, transaction_hash, quiz_id')\
                .gte('timestamp', start_datetime)\
                .lte('timestamp', end_datetime)\
                .eq('status', True)\
                .order('timestamp', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get learn earn participants"
        )

        formatted_participants = []
        total_g_disbursed = 0
        total_achievement_cards = 0

        if participants and participants.data:
            logger.info(f"✅ Found {len(participants.data)} Learn & Earn participants")
            for p in participants.data:
                wallet = p.get('wallet_address', '')
                amount = float(p.get('amount_g$', 0))
                total_g_disbursed += amount
                total_achievement_cards += 1

                formatted_participants.append({
                    'wallet_address': wallet,
                    'display_name': f"{wallet[:6]}...{wallet[-4:]}",
                    'amount_g$': amount,
                    'amount_formatted': f"{amount:,.1f} G$",
                    'achievement_card_label': 'Convertible into NFT',
                    'timestamp': p.get('timestamp'),
                    'transaction_hash': p.get('transaction_hash', 'N/A'),
                    'quiz_id': p.get('quiz_id', 'N/A')
                })
        else:
            logger.info(f"ℹ️ No Learn & Earn participants found for {target_date or 'today'}")

        return jsonify({
            "success": True,
            "participants": formatted_participants,
            "total_count": len(formatted_participants),
            "total_g_disbursed": total_g_disbursed,
            "total_g_disbursed_formatted": f"{total_g_disbursed:,.2f} G$",
            "total_achievement_cards": total_achievement_cards,
            "total_achievement_cards_formatted": f"{total_achievement_cards:,} card{'s' if total_achievement_cards != 1 else ''}",
            "date": target_date if target_date else datetime.utcnow().strftime('%Y-%m-%d')
        })

    except Exception as e:
        logger.error(f"❌ Error getting Learn & Earn participants: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({
            "success": False,
            "participants": [],
            "total_count": 0,
            "total_g_disbursed": 0,
            "total_achievement_cards": 0,
            "total_achievement_cards_formatted": "0 cards",
            "error": str(e)
        })

@routes.route("/api/achievement-card-sales", methods=["GET"])
def get_all_achievement_card_sales():
    """Get ALL platform-wide achievement card sales for the overview analytics page"""
    try:
        from supabase_client import get_supabase_client

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "sales": [], "error": "Database not available"}), 500

        limit = int(request.args.get('limit', 100))

        result = supabase.table('achievement_card_sales')\
            .select('wallet_address, quiz_id, score, total_questions, sell_price, transaction_hash, created_at')\
            .order('created_at', desc=True)\
            .limit(limit)\
            .execute()

        sales = result.data if result.data else []
        total_earned = sum(float(s.get('sell_price', 0)) for s in sales)
        unique_sellers = len(set(s.get('wallet_address', '') for s in sales))

        formatted = []
        for s in sales:
            wallet = s.get('wallet_address', '')
            formatted.append({
                'wallet_address': wallet,
                'display_name': f"{wallet[:6]}...{wallet[-4:]}" if len(wallet) > 10 else wallet,
                'score': s.get('score'),
                'total_questions': s.get('total_questions'),
                'sell_price': float(s.get('sell_price', 0)),
                'transaction_hash': s.get('transaction_hash'),
                'created_at': s.get('created_at'),
            })

        logger.info(f"📜 Platform achievement card sales: {len(sales)} records, {unique_sellers} unique sellers, total {total_earned} G$")

        return jsonify({
            "success": True,
            "sales": formatted,
            "sale_count": len(sales),
            "unique_sellers": unique_sellers,
            "total_earned": total_earned,
            "total_earned_formatted": f"{total_earned:,.2f} G$"
        })

    except Exception as e:
        logger.error(f"❌ Error fetching all achievement card sales: {e}")
        return jsonify({"success": False, "sales": [], "error": str(e)}), 500


@routes.route("/api/screenshot/<path:filename>", methods=["GET"])
def serve_screenshot(filename):
    """Serve screenshot from Object Storage"""
    try:
        from object_storage_client import download_screenshot
        from flask import send_file
        import io

        # Download from Object Storage
        file_data = download_screenshot(filename)

        if not file_data:
            return jsonify({"success": False, "error": "Screenshot not found"}), 404

        # Return as image
        return send_file(
            io.BytesIO(file_data),
            mimetype='image/png',
            as_attachment=False
        )

    except Exception as e:
        logger.error(f"❌ Error serving screenshot: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/community-screenshots", methods=["GET"])
def get_community_screenshots():
    """Get community screenshots for homepage"""
    try:
        from community_stories.community_stories_service import community_stories_service
        from supabase_client import get_supabase_client
        from cache_utils import api_cache

        # Check cache first (2 minute TTL)
        cache_key = "community_screenshots"
        cached_result = api_cache.get(cache_key)
        if cached_result:
            return jsonify(cached_result)

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "screenshots": []})

        limit = int(request.args.get('limit', 12))

        result = community_stories_service.get_screenshots_for_homepage(limit)

        if result.get('success') and result.get('screenshots'):
            # Display names are now just wallet truncations (no username lookup)
            for screenshot in result['screenshots']:
                wallet = screenshot.get('wallet_address', '')
                screenshot['display_name'] = f"{wallet[:6]}...{wallet[-4:]}"

        # Cache for 2 minutes for better performance
        api_cache.set(cache_key, result, ttl=120)

        return jsonify(result)

    except Exception as e:
        logger.error(f"❌ Error getting community screenshots: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/recent-community-stories", methods=["GET"])
def get_recent_community_stories():
    """Get recent approved community stories"""
    try:
        from supabase_client import get_supabase_client

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "stories": []})

        limit = int(request.args.get('limit', 50))

        # Get approved community stories (both high and low rewards)
        stories = safe_supabase_operation(
            lambda: supabase.table('community_stories_submissions')\
                .select('*')\
                .in_('status', ['approved_high', 'approved_low'])\
                .order('reviewed_at', desc=True)\
                .limit(limit)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get recent community stories"
        )

        # Format stories without username
        formatted_stories = []
        if stories and stories.data:
            for story in stories.data:
                wallet = story.get('wallet_address', '')

                formatted_stories.append({
                    'wallet_address': wallet,
                    'display_name': f"{wallet[:6]}...{wallet[-4:]}",
                    'reward_amount': float(story.get('reward_amount', 0)),
                    'reviewed_at': story.get('reviewed_at'),
                    'status': story.get('status'),
                    'tweet_url': story.get('tweet_url', ''),
                    'submission_id': story.get('submission_id')
                })

        return jsonify({
            "success": True,
            "stories": formatted_stories,
            "total_count": len(formatted_stories)
        })

    except Exception as e:
        logger.error(f"❌ Error getting recent community stories: {e}")
        return jsonify({"success": False, "stories": []})

@routes.route("/api/admin/maintenance-status", methods=["GET"])
@admin_required
def get_maintenance_status_api():
    feature = request.args.get('feature', 'wallet_connection')
    from maintenance_service import maintenance_service
    result = maintenance_service.get_maintenance_status(feature)
    return jsonify(result)

@routes.route("/api/admin/maintenance-status", methods=["POST"])
@admin_required
def set_maintenance_status_api():
    data = request.get_json()
    feature_name = data.get('feature_name')
    is_maintenance = data.get('is_maintenance')
    message = data.get('message')
    admin_wallet = session.get('wallet')

    from maintenance_service import maintenance_service
    result = maintenance_service.set_maintenance_status(feature_name, is_maintenance, message, admin_wallet)
    return jsonify(result)

@routes.route("/api/maintenance-status", methods=["GET"])
def public_maintenance_status():
    feature = request.args.get('feature', 'wallet_connection')
    wallet_address = request.args.get('wallet') # Get wallet from query param for exemption check

    from maintenance_service import maintenance_service
    result = maintenance_service.get_maintenance_status(feature)

    # Check if the specific wallet provided is an admin
    check_wallet = wallet_address or session.get('wallet')

    if check_wallet:
        from supabase_client import is_admin
        if is_admin(check_wallet):
            logger.info(f"🛡️ Admin {check_wallet[:8]}... detected, bypassing maintenance for {feature}")
            result['is_maintenance'] = False
            result['message'] = ""

    return jsonify(result)

@routes.route("/api/price-visibility", methods=["GET"])
def public_price_visibility():
    """Public endpoint to check if live price display is enabled"""
    try:
        now = time.time()
        if _price_visibility_cache["data"] is not None and now < _price_visibility_cache["expires"]:
            return jsonify(_price_visibility_cache["data"])

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "show_price": True})
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('is_maintenance')
                .eq('feature_name', 'live_price_display')
                .execute(),
            operation_name="get price visibility"
        )
        if result and result.data:
            is_hidden = result.data[0].get('is_maintenance', False)
            data = {"success": True, "show_price": not is_hidden}
        else:
            data = {"success": True, "show_price": True}

        _price_visibility_cache["data"] = data
        _price_visibility_cache["expires"] = now + _PUBLIC_ENDPOINT_CACHE_TTL
        return jsonify(data)
    except Exception as e:
        logger.error(f"Price visibility fetch error: {e}")
        return jsonify({"success": True, "show_price": True})

@routes.route("/api/admin/price-visibility", methods=["GET"])
@admin_required
def get_price_visibility():
    """Get current price visibility setting"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('is_maintenance')
                .eq('feature_name', 'live_price_display')
                .execute(),
            operation_name="get price visibility admin"
        )
        if result and result.data:
            is_hidden = result.data[0].get('is_maintenance', False)
            return jsonify({"success": True, "show_price": not is_hidden})
        return jsonify({"success": True, "show_price": True})
    except Exception as e:
        logger.error(f"Admin price visibility fetch error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/price-visibility", methods=["POST"])
@admin_required
def set_price_visibility():
    """Toggle live price display on/off"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500
        data = request.get_json()
        show_price = data.get('show_price', True)
        is_hidden = not show_price
        admin_wallet = session.get('wallet')

        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('feature_name')
                .eq('feature_name', 'live_price_display')
                .execute(),
            operation_name="check price visibility row"
        )

        if existing and existing.data:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .update({'is_maintenance': is_hidden})
                    .eq('feature_name', 'live_price_display')
                    .execute(),
                operation_name="update price visibility"
            )
        else:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .insert({'feature_name': 'live_price_display', 'is_maintenance': is_hidden, 'maintenance_message': ''})
                    .execute(),
                operation_name="insert price visibility"
            )

        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="update_price_visibility",
            action_details={"show_price": show_price}
        )

        _price_visibility_cache["data"] = None
        _price_visibility_cache["expires"] = 0
        return jsonify({"success": True, "show_price": show_price})
    except Exception as e:
        logger.error(f"Set price visibility error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/feature-visibility", methods=["GET"])
def public_feature_visibility():
    """Public endpoint to check if swap/wallet features are visible to users"""
    try:
        now = time.time()
        if _feature_visibility_cache["data"] is not None and now < _feature_visibility_cache["expires"]:
            return jsonify(_feature_visibility_cache["data"])

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "swap_visible": True, "wallet_visible": True,
                            "savings_visible": True, "topup_visible": True, "giftcard_visible": True,
                            "virtualcard_visible": True, "utility_visible": True,
                            "reserve_swap_visible": False, "buy_eth_visible": True})
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('feature_name,is_maintenance')
                .in_('feature_name', ['swap_feature', 'wallet_feature', 'savings_feature', 'store_topup', 'store_giftcard', 'store_virtualcard', 'store_utility', 'reserve_swap_feature', 'wallet_buy_eth'])
                .execute(),
            operation_name="get feature visibility"
        )
        swap_visible = True
        wallet_visible = True
        savings_visible = True
        topup_visible = True
        giftcard_visible = True
        virtualcard_visible = True
        utility_visible = True
        reserve_swap_visible = False
        buy_eth_visible = True
        if result and result.data:
            for row in result.data:
                fn = row['feature_name']
                val = not row.get('is_maintenance', False)
                if fn == 'swap_feature':
                    swap_visible = val
                elif fn == 'wallet_feature':
                    wallet_visible = val
                elif fn == 'savings_feature':
                    savings_visible = val
                elif fn == 'store_topup':
                    topup_visible = val
                elif fn == 'store_giftcard':
                    giftcard_visible = val
                elif fn == 'store_virtualcard':
                    virtualcard_visible = val
                elif fn == 'store_utility':
                    utility_visible = val
                elif fn == 'reserve_swap_feature':
                    reserve_swap_visible = val
                elif fn == 'wallet_buy_eth':
                    buy_eth_visible = val
        data = {"success": True, "swap_visible": swap_visible, "wallet_visible": wallet_visible,
                "savings_visible": savings_visible,
                "topup_visible": topup_visible, "giftcard_visible": giftcard_visible,
                "virtualcard_visible": virtualcard_visible, "utility_visible": utility_visible,
                "reserve_swap_visible": reserve_swap_visible,
                "buy_eth_visible": buy_eth_visible}
        _feature_visibility_cache["data"] = data
        _feature_visibility_cache["expires"] = now + _PUBLIC_ENDPOINT_CACHE_TTL
        return jsonify(data)
    except Exception as e:
        logger.error(f"Feature visibility fetch error: {e}")
        return jsonify({"success": True, "swap_visible": True, "wallet_visible": True,
                        "savings_visible": True, "topup_visible": True, "giftcard_visible": True,
                        "virtualcard_visible": True, "utility_visible": True,
                        "reserve_swap_visible": False, "buy_eth_visible": True})


@routes.route("/api/admin/feature-visibility", methods=["GET"])
@admin_required
def get_feature_visibility():
    """Admin: get current visibility settings for swap and wallet features"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('feature_name,is_maintenance')
                .in_('feature_name', ['swap_feature', 'wallet_feature', 'savings_feature', 'store_topup', 'store_giftcard', 'store_virtualcard', 'store_utility', 'reserve_swap_feature', 'wallet_buy_eth'])
                .execute(),
            operation_name="get feature visibility admin"
        )
        swap_visible = True
        wallet_visible = True
        savings_visible = True
        topup_visible = True
        giftcard_visible = True
        virtualcard_visible = True
        utility_visible = True
        reserve_swap_visible = False
        buy_eth_visible = True
        if result and result.data:
            for row in result.data:
                fn = row['feature_name']
                val = not row.get('is_maintenance', False)
                if fn == 'swap_feature':
                    swap_visible = val
                elif fn == 'wallet_feature':
                    wallet_visible = val
                elif fn == 'savings_feature':
                    savings_visible = val
                elif fn == 'store_topup':
                    topup_visible = val
                elif fn == 'store_giftcard':
                    giftcard_visible = val
                elif fn == 'store_virtualcard':
                    virtualcard_visible = val
                elif fn == 'store_utility':
                    utility_visible = val
                elif fn == 'reserve_swap_feature':
                    reserve_swap_visible = val
                elif fn == 'wallet_buy_eth':
                    buy_eth_visible = val
        return jsonify({"success": True, "swap_visible": swap_visible, "wallet_visible": wallet_visible,
                        "savings_visible": savings_visible,
                        "topup_visible": topup_visible, "giftcard_visible": giftcard_visible,
                        "virtualcard_visible": virtualcard_visible, "utility_visible": utility_visible,
                        "reserve_swap_visible": reserve_swap_visible,
                        "buy_eth_visible": buy_eth_visible})
    except Exception as e:
        logger.error(f"Admin feature visibility fetch error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/feature-visibility", methods=["POST"])
@admin_required
def set_feature_visibility():
    """Admin: toggle visibility of swap or wallet feature"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500
        data = request.get_json()
        feature = data.get('feature')
        visible = data.get('visible', True)
        is_hidden = not visible
        admin_wallet = session.get('wallet')

        if feature not in ['swap_feature', 'wallet_feature', 'savings_feature', 'store_topup', 'store_giftcard', 'store_virtualcard', 'store_utility', 'reserve_swap_feature', 'wallet_buy_eth']:
            return jsonify({"success": False, "error": "Invalid feature name"}), 400

        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')
                .select('feature_name')
                .eq('feature_name', feature)
                .execute(),
            operation_name="check feature visibility row"
        )

        if existing and existing.data:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .update({'is_maintenance': is_hidden})
                    .eq('feature_name', feature)
                    .execute(),
                operation_name="update feature visibility"
            )
        else:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .insert({'feature_name': feature, 'is_maintenance': is_hidden, 'maintenance_message': ''})
                    .execute(),
                operation_name="insert feature visibility"
            )

        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="update_feature_visibility",
            action_details={"feature": feature, "visible": visible}
        )

        _feature_visibility_cache["data"] = None
        _feature_visibility_cache["expires"] = 0
        return jsonify({"success": True, "feature": feature, "visible": visible})
    except Exception as e:
        logger.error(f"Set feature visibility error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/")
def index():
    """Main homepage with Connect Wallet style"""
    # If already logged in, redirect to wallet
    if session.get("verified") and session.get("wallet"):
        return redirect("/wallet")
    wc_project_id = os.environ.get("WALLETCONNECT_PROJECT_ID", "")
    try:
        homepage_stats = analytics.get_homepage_public_stats()
    except Exception as e:
        logger.error(f"homepage stats failed: {e}")
        homepage_stats = {
            "total_g_disbursed_formatted": "—",
            "total_g_disbursed_week_growth_pct": None,
            "active_earners_formatted": "—",
            "tasks_last_30_days_formatted": "—",
        }
    return render_template(
        "homepage.html",
        walletconnect_project_id=wc_project_id,
        walletconnect_sidecar_enabled=_is_walletconnect_sidecar_enabled(),
        homepage_stats=homepage_stats,
    )


@routes.route("/api/homepage-stats")
def api_homepage_stats():
    """Public JSON endpoint for homepage hero stats (no auth required)."""
    try:
        return jsonify({"success": True, "stats": analytics.get_homepage_public_stats()})
    except Exception as e:
        logger.error(f"homepage stats endpoint failed: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/login")
def login_page():
    """Old login page - redirect to new homepage"""
    if session.get('verified') and session.get('wallet'):
        return redirect(url_for("routes.dashboard"))
    return redirect(url_for("routes.index"))

@routes.route("/login", methods=["POST"])
def login():
    """Legacy login endpoint - redirects to main page"""
    # This legacy login should ideally be updated or removed
    # For now, assuming it sets session['wallet'] and session['verified'] if needed
    # For the purpose of this edit, we assume session['wallet'] is set by other means if this is bypassed
    # If session['wallet'] is not set, the subsequent checks will handle redirection.
    return redirect(url_for("routes.index"))

@routes.route("/verify-ubi-page")
def verify_ubi_page():
    """Legacy verify page - redirects to main page"""
    return redirect(url_for("routes.index"))

def _disburse_referral_rewards(referral_blockchain_service, referral_service,
                               referrer_wallet, referee_wallet, referral_code):
    """Thin wrapper that delegates to ReferralService.process_referral_disbursement.

    The blockchain service argument is kept for backward-compatibility with
    existing call sites; the service method imports it on its own so the
    parameter is intentionally unused here.
    """
    del referral_blockchain_service  # imported inside the service method
    return referral_service.process_referral_disbursement(
        referrer_wallet=referrer_wallet,
        referee_wallet=referee_wallet,
        referral_code=referral_code,
    )


@routes.route("/verify-ubi", methods=["POST"])
def verify_ubi():
    try:
        data = request.get_json()
        wallet_address = data.get("wallet", "").strip()
        referral_code = data.get("referral_code", None) # Get referral code from request
        track_analytics = data.get("track_analytics", False)

        if not wallet_address:
            return jsonify({"status": "error", "message": "⚠️ Wallet address required"}), 400

        # Normalize to EIP-55 checksum format so MetaMask, WalletConnect,
        # and manual paste all resolve to the SAME record in Supabase
        if Web3.is_address(wallet_address):
            try:
                wallet_address = Web3.to_checksum_address(wallet_address)
            except Exception:
                pass

        # UBI check temporarily disabled — allow all wallets in
        if True:
            # Track successful verification (for GoodMarket access)
            analytics.track_verification_attempt(wallet_address, True)
            analytics.track_user_session(wallet_address)

            # Store in session
            session["wallet"] = wallet_address
            session["verified"] = True
            session["login_method"] = "walletconnect"

            # Check actual GoodDollar face verification status.
            # Even though we allow all wallets in, we still want to track
            # users who are NOT yet face-verified so we can count how many
            # eventually verify after discovering GoodMarket.
            fv_result = {'verified': False}
            try:
                from blockchain import is_identity_verified
                from supabase_client import supabase_logger
                fv_result = is_identity_verified(wallet_address)
                if not fv_result.get('verified', False) and supabase_logger:
                    supabase_logger.record_unverified_visit(wallet_address)
                    logger.info(f"📝 New unverified visitor recorded: {wallet_address[:8]}...")
                elif fv_result.get('verified', False):
                    logger.info(f"✅ User is already face-verified: {wallet_address[:8]}...")
                    # Mark face_verified + ubi_verified in user_data
                    if supabase_logger:
                        supabase_logger.log_verification_attempt(
                            wallet_address, success=True, face_verified=True
                        )
            except Exception as fv_check_err:
                logger.warning(f"⚠️ Could not check face verification status for tracking: {fv_check_err}")

            # Placeholder values since UBI check is skipped
            block_number = "N/A"
            claim_amount = "N/A"

            # Referral Program Processing
            # Rewards are ONLY disbursed when the referee (invited user) is face-verified.
            # Inviter (referrer) gets 1000 G$, Invited user (referee) gets 500 G$.
            # If REFERRAL_KEY runs out, rewards are marked pending_disbursed and auto-retried.
            try:
                from referral_program.referral_service import referral_service
                from referral_program.blockchain import referral_blockchain_service

                is_face_verified = fv_result.get('verified', False)

                # --- Case 1: New referral code provided ---
                if referral_code and referral_code.strip():
                    logger.info(f"Referral code provided: {referral_code} for {wallet_address[:8]}... face_verified={is_face_verified}")

                    validation = referral_service.validate_referral_code(referral_code.strip().upper())
                    if not validation.get('valid'):
                        logger.warning(f"Invalid referral code {referral_code}: {validation.get('error')}")
                    else:
                        referrer_wallet = validation['referrer_wallet']
                        record_result = referral_service.record_referral(
                            referral_code=referral_code.strip().upper(),
                            referee_wallet=wallet_address
                        )
                        if record_result.get('already_verified'):
                            logger.info(
                                f"Referral rejected (already verified externally): {wallet_address[:8]}... "
                                f"code={referral_code}"
                            )
                        elif record_result.get('success'):
                            logger.info(f"Referral recorded: {referral_code} | referrer={referrer_wallet[:8]}...")
                            if is_face_verified:
                                _disburse_referral_rewards(
                                    referral_blockchain_service, referral_service,
                                    referrer_wallet, wallet_address, referral_code.strip().upper()
                                )
                            else:
                                logger.info(
                                    f"Referee {wallet_address[:8]}... not yet face-verified. "
                                    f"Referral pending face verification."
                                )
                        elif record_result.get('already_exists'):
                            logger.info(f"Referral already recorded for {wallet_address[:8]}... checking if pending disbursement needed.")
                            if is_face_verified:
                                claimed = referral_service.claim_pending_referral_for_disbursement(wallet_address)
                                if claimed.get('claimed'):
                                    pending_row = claimed['referral']
                                    logger.info(
                                        f"Pending referral claimed for {wallet_address[:8]}... "
                                        f"disbursing now (code={pending_row['referral_code']})."
                                    )
                                    _disburse_referral_rewards(
                                        referral_blockchain_service, referral_service,
                                        pending_row['referrer_wallet'], wallet_address,
                                        pending_row['referral_code']
                                    )
                                else:
                                    logger.info(f"No pending referral to claim for {wallet_address[:8]}... (already completed, failed, or being processed).")
                        else:
                            logger.warning(f"Could not record referral: {record_result.get('error')}")

                # --- Case 2: No new code, but wallet has a pending referral and is now face-verified ---
                elif is_face_verified:
                    claimed = referral_service.claim_pending_referral_for_disbursement(wallet_address)
                    if claimed.get('claimed'):
                        ref_row = claimed['referral']
                        referrer_wallet = ref_row['referrer_wallet']
                        ref_code = ref_row['referral_code']
                        logger.info(
                            f"Referee {wallet_address[:8]}... is now face-verified. "
                            f"Disbursing pending referral rewards (code={ref_code})."
                        )
                        _disburse_referral_rewards(
                            referral_blockchain_service, referral_service,
                            referrer_wallet, wallet_address, ref_code
                        )

            except Exception as ref_error:
                logger.error(f"Referral processing error: {ref_error}")
                logger.exception("Referral error traceback:")

            # Set permanent session
            session.permanent = True

            return jsonify({
                'success': True,
                'status': 'success',
                'message': 'Identity verification successful!',
                'wallet': wallet_address,
                'ubi_verified': True,
                'redirect_to': '/wallet'
            })

    except Exception as e:
        logger.exception("Verification error occurred")
        # Return custom message instead of generic error
        error_message = "You need to claim G$ once every 24 hours to access GoodMarket.\n\nClaim G$ using:\n• MiniPay app (built into Opera Mini)\n• goodwallet.xyz\n• gooddapp.org"
        return jsonify({
            "status": "error",
            "message": error_message,
            "reason": "verification_error"
        }), 500

@routes.route("/overview")
def overview():
    wallet = session.get('wallet') or session.get('wallet_address')
    verified = session.get('verified') or session.get('ubi_verified')
    username = None

    # Check if user has valid session (UBI check disabled)
    if wallet and verified:
        # Track overview page visit asynchronously (don't block render)
        import threading
        threading.Thread(target=analytics.track_page_view, args=(wallet, "overview"), daemon=True).start()

    # Get analytics - pass None for guest users, wallet for authenticated users
    stats = analytics.get_dashboard_stats(wallet if wallet and verified else None)

    # Debug logging
    logger.debug(f"🔍 Overview page - Wallet: {wallet[:8] if wallet else 'Guest'}...")
    logger.debug(f"🔍 Overview page - stats keys: {list(stats.keys())}")
    logger.debug(f"🔍 Overview page - disbursement_analytics present: {'disbursement_analytics' in stats}")
    if 'disbursement_analytics' in stats:
        logger.debug(f"🔍 Overview page - disbursement_analytics keys: {list(stats['disbursement_analytics'].keys())}")
        logger.debug(f"🔍 Overview page - breakdown_formatted present: {'breakdown_formatted' in stats['disbursement_analytics']}")

    import os as _os
    return render_template("overview.html",
                         wallet=wallet if wallet and verified else None,
                         data=stats,
                         login_method=session.get("login_method", "walletconnect"))

@routes.route("/dashboard")
def dashboard():
    """Dashboard page"""
    wallet = session.get('wallet') or session.get('wallet_address')
    verified = session.get('verified') or session.get('ubi_verified')

    if not wallet or not verified:
        return redirect(url_for("routes.index"))

    # Check on-chain Face Verification (GoodDollar Identity contract)
    try:
        from blockchain import is_identity_verified
        fv_result = is_identity_verified(wallet)
        if not fv_result.get("verified", False):
            return redirect(url_for("routes.wallet_page") + "?fv_required=1")
    except Exception as e:
        logger.warning(f"⚠️ Could not check FV status for dashboard access: {e}")

    # Track dashboard visit asynchronously (don't block render)
    import threading
    threading.Thread(target=analytics.track_page_view, args=(wallet, "dashboard"), daemon=True).start()

    import os as _os
    try:
        homepage_stats = analytics.get_homepage_public_stats()
    except Exception as e:
        logger.error(f"dashboard stats failed: {e}")
        homepage_stats = {
            "total_g_disbursed_formatted": "���",
            "total_g_disbursed_week_growth_pct": None,
            "active_earners_formatted": "—",
            "tasks_last_30_days_formatted": "—",
        }
    return render_template("dashboard.html", wallet=wallet, homepage_stats=homepage_stats)


@routes.route("/api/user/username", methods=["GET"])
@auth_required
def get_user_username():
    """Get current username and edit eligibility (once per year)."""
    try:
        wallet = session.get("wallet")
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 500

        user_result = safe_supabase_operation(
            lambda: supabase.table("user_data")
                .select("wallet_address, username, username_set_at, username_last_edited")
                .ilike("wallet_address", wallet)
                .limit(1)
                .execute(),
            operation_name="fetch username profile"
        )

        if not user_result or not user_result.data:
            return jsonify({
                "success": True,
                "username": None,
                "can_edit": True,
                "next_edit_at": None,
                "days_until_edit": 0
            })

        user = user_result.data[0]
        username = user.get("username")
        last_edit_dt = _parse_iso_datetime(user.get("username_last_edited")) or _parse_iso_datetime(user.get("username_set_at"))

        can_edit = True
        next_edit_at = None
        days_until_edit = 0

        if username and last_edit_dt:
            next_allowed_dt = last_edit_dt + timedelta(days=365)
            now_utc = datetime.now(timezone.utc)
            if now_utc < next_allowed_dt:
                can_edit = False
                next_edit_at = next_allowed_dt.isoformat()
                delta = next_allowed_dt - now_utc
                days_until_edit = max(1, int((delta.total_seconds() + 86399) // 86400))

        return jsonify({
            "success": True,
            "username": username,
            "can_edit": can_edit,
            "next_edit_at": next_edit_at,
            "days_until_edit": days_until_edit
        })
    except Exception as e:
        logger.error(f"❌ Error getting username: {e}")
        return jsonify({"success": False, "error": "Failed to load username"}), 500


@routes.route("/api/user/username", methods=["POST"])
@auth_required
def update_user_username():
    """Set or update username with once-per-year edit limit."""
    try:
        wallet = session.get("wallet")
        data = request.get_json() or {}
        raw_username = (data.get("username") or "").strip()

        if not raw_username:
            return jsonify({"success": False, "error": "Username is required"}), 400

        if not re.fullmatch(r"[A-Za-z0-9_]{3,24}", raw_username):
            return jsonify({
                "success": False,
                "error": "Username must be 3-24 characters and contain only letters, numbers, and underscore (_)"
            }), 400

        normalized_username = raw_username.lower()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 500

        existing_username = safe_supabase_operation(
            lambda: supabase.table("user_data")
                .select("wallet_address, username")
                .ilike("username", normalized_username)
                .limit(1)
                .execute(),
            operation_name="check username uniqueness"
        )

        if existing_username and existing_username.data:
            owner_wallet = (existing_username.data[0].get("wallet_address") or "").lower()
            if owner_wallet != (wallet or "").lower():
                return jsonify({"success": False, "error": "Username is already taken"}), 409

        user_result = safe_supabase_operation(
            lambda: supabase.table("user_data")
                .select("id, wallet_address, username, username_set_at, username_last_edited")
                .ilike("wallet_address", wallet)
                .limit(1)
                .execute(),
            operation_name="fetch user for username update"
        )

        now_utc = datetime.now(timezone.utc)
        now_iso = now_utc.isoformat()

        if user_result and user_result.data:
            user = user_result.data[0]
            current_username = (user.get("username") or "").strip()
            if current_username.lower() == normalized_username:
                return jsonify({
                    "success": True,
                    "username": current_username,
                    "message": "Username is already set to that value"
                })

            if current_username:
                last_edit_dt = _parse_iso_datetime(user.get("username_last_edited")) or _parse_iso_datetime(user.get("username_set_at"))
                if last_edit_dt:
                    next_allowed_dt = last_edit_dt + timedelta(days=365)
                    if now_utc < next_allowed_dt:
                        return jsonify({
                            "success": False,
                            "error": "Username can only be edited once per year",
                            "next_edit_at": next_allowed_dt.isoformat()
                        }), 429

            update_data = {
                "username": normalized_username,
                "username_last_edited": now_iso
            }
            if not user.get("username_set_at"):
                update_data["username_set_at"] = now_iso
            if current_username:
                update_data["username_edited"] = True

            update_result = safe_supabase_operation(
                lambda: supabase.table("user_data")
                    .update(update_data)
                    .eq("id", user["id"])
                    .execute(),
                operation_name="update username"
            )
            if not update_result:
                return jsonify({"success": False, "error": "Failed to update username"}), 500
        else:
            insert_data = {
                "wallet_address": wallet,
                "username": normalized_username,
                "username_set_at": now_iso,
                "username_last_edited": now_iso
            }
            insert_result = safe_supabase_operation(
                lambda: supabase.table("user_data").insert(insert_data).execute(),
                operation_name="create user with username"
            )
            if not insert_result:
                return jsonify({"success": False, "error": "Failed to save username"}), 500

        session["username"] = normalized_username
        return jsonify({
            "success": True,
            "username": normalized_username,
            "message": "Username updated successfully"
        })
    except Exception as e:
        logger.error(f"❌ Error updating username: {e}")
        return jsonify({"success": False, "error": "Failed to update username"}), 500

@routes.route("/track-analytics", methods=["POST"])
def track_analytics_endpoint(): # Renamed to avoid conflict with analytics_service
    try:
        data = request.get_json()
        if not data:
            logger.error("❌ track-analytics: No JSON data received")
            return jsonify({"status": "error", "message": "No data provided"}), 400

        event = data.get("event")
        wallet = data.get("wallet")
        # Add username to track if available in request data
        username = data.get("username")

        logger.info(f"🔍 track-analytics: event='{event}', wallet='{wallet}', username='{username}'")

        if event and wallet:
            # Track page view (analytics.track_page_view only takes wallet and page)
            analytics.track_page_view(wallet, event)
            return jsonify({"status": "success"})

        missing = []
        if not event:
            missing.append("event")
        if not wallet:
            missing.append("wallet")

        error_msg = f"Missing required fields: {', '.join(missing)}"
        logger.error(f"❌ track-analytics: {error_msg}")
        return jsonify({"status": "error", "message": error_msg}), 400

    except Exception as e:
        logger.exception("❌ track-analytics error") # Use logger.exception for full traceback
        return jsonify({"status": "error", "message": str(e)}), 500

@routes.route("/ubi-tracker")
def ubi_tracker_page():
    if not session.get("verified") or not session.get("wallet"):
        return redirect(url_for("routes.index"))

    wallet = session.get("wallet")

    analytics.track_page_view(wallet, "ubi_tracker")

    return render_template("ubi_tracker.html",
                         wallet=wallet,
                         contract_count=len(GOODDOLLAR_CONTRACTS))

@routes.route("/logout")
def logout():
    wallet = session.get("wallet")
    if wallet:
        # Log logout to Supabase
        supabase_logger.log_logout(wallet)

    # Completely clear the session
    session.clear()

    # Create response with redirect
    response = redirect(url_for("routes.index"))

    # Clear all session cookies
    response.set_cookie('session', '', expires=0, path='/')
    response.set_cookie('wallet', '', expires=0, path='/')
    response.set_cookie('verified', '', expires=0, path='/')
    response.set_cookie('username', '', expires=0, path='/')

    # Add cache control headers to prevent caching
    response.headers['Cache-Control'] = 'no-cache, no-store, must-revalidate, private'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'

    return response


@routes.route("/news")
def news_feed_page():
    wallet = session.get("wallet")

    # Track news page visit only for logged-in users
    if wallet and session.get("verified"):
        analytics.track_page_view(wallet, "news_feed")

    # Get news feed data for initial page load
    from news_feed import news_feed_service

    featured_news = news_feed_service.get_featured_news(limit=3)
    recent_news = news_feed_service.get_news_feed(limit=10)
    news_stats = news_feed_service.get_news_stats()

    return render_template("news_feed.html",
                         wallet=wallet,
                         featured_news=featured_news,
                         recent_news=recent_news,
                         news_stats=news_stats,
                         categories=news_feed_service.categories)

@routes.route('/news/article/<article_id>')
def news_article_page(article_id: str):
    """Individual news article page"""
    from news_feed import news_feed_service # Import moved here to avoid circular import issues if news_feed is used elsewhere before this route is called

    article = news_feed_service.get_news_article(article_id)

    if not article:
        # return render_template("404.html"), 404 # Assuming a 404 template exists
        return "Article not found", 404

    # Get the full article URL for sharing
    article_url = request.url

    # Prepare meta tags for social media sharing - this is now handled by passing article_url to the template
    # meta_tags = {
    #     "title": article.get('title', 'GoodDollar News'),
    #     "description": article.get('content', '')[:200], # Truncate description
    #     "image": article.get('image_url', ''),
    #     "url": article_url # Use the correctly constructed article URL
    # }

    # Add any additional session/wallet checks if this page requires authentication
    wallet = session.get("wallet")
    verified = session.get("verified")
    username = None
    if wallet and verified:
        # username = supabase_logger.get_username(wallet) # Username fetching moved to template rendering if needed
        analytics.track_page_view(wallet, f"news_article_{article_id}")

    return render_template("news_article.html",
                         article=article,
                         article_url=article_url, # Pass article_url to template
                         wallet=wallet if wallet and verified else None,
                         username=username if username else "Guest")


@routes.route("/api/admin/check", methods=["GET"])
@auth_required
def check_admin_status():
    """Check if current user is admin"""
    try:
        wallet = session.get("wallet")
        from supabase_client import is_admin

        is_admin_user = is_admin(wallet)

        return jsonify({
            "success": True,
            "is_admin": is_admin_user,
            "wallet": wallet[:8] + "..." if wallet else None
        })
    except Exception as e:
        logger.error(f"❌ Admin check error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/backfill-gm-attribution", methods=["POST", "GET"])
@admin_required
def admin_backfill_gm_attribution():
    """Admin: backfill ``user_data.verified_after_goodmarket`` for every wallet
    that has GoodMarket-claim activity but isn't yet attributed.

    Query params (also accepts JSON body for POST):
        * ``dry_run`` — when ``true`` / ``1``, no writes; returns the impact.
        * ``limit``   — cap candidates examined this run (defaults to the
          module's ``MAX_WALLETS_PER_RUN``).

    Auth: protected by ``@admin_required`` (same as every other admin route).
    Audit: writes to ``admin_action_logs`` so we can see who triggered each run.
    """
    try:
        # Accept both query params and JSON body so curl + dashboard buttons
        # both work without ceremony.
        body = request.get_json(silent=True) or {}
        raw_dry = (request.args.get("dry_run") or body.get("dry_run") or "")
        dry_run = str(raw_dry).strip().lower() in ("1", "true", "yes", "on")

        raw_limit = request.args.get("limit") or body.get("limit")
        limit = None
        if raw_limit is not None and str(raw_limit).strip() != "":
            try:
                limit = max(1, int(raw_limit))
            except (TypeError, ValueError):
                return jsonify({
                    "success": False,
                    "error": "limit must be an integer"
                }), 400

        from goodmarket_attribution_backfill import run_full_backfill
        summary = run_full_backfill(dry_run=dry_run, limit=limit)

        # Audit log — best-effort. Don't fail the request if logging fails.
        try:
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="backfill_gm_attribution",
                action_details={
                    "dry_run": dry_run,
                    "limit": limit,
                    "examined": summary.get("examined"),
                    "updated": summary.get("updated"),
                    "errors": summary.get("errors"),
                },
            )
        except Exception as audit_err:
            logger.warning(f"[gm-backfill] admin audit log skipped: {audit_err}")

        return jsonify(summary), (200 if summary.get("success") else 500)
    except Exception as e:
        logger.error(f"[gm-backfill] admin endpoint error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/attribution-correct", methods=["POST", "GET"])
@admin_required
def admin_attribution_correct():
    """Admin: re-evaluate every ``user_data`` row that currently has
    ``verified_after_goodmarket = TRUE`` against the strict attribution rule
    and unset the flag where it no longer qualifies.

    Use this once after deploying the strict-attribution change to clean up
    rows that the legacy "any whitelisted user counts" code wrote. NEVER
    auto-runs — admin must explicitly trigger.

    Query params (also accepts JSON body):
        * ``dry_run`` — defaults to TRUE. Pass ``false`` / ``0`` to actually
          write the corrections. Always start with a dry run to preview.
        * ``limit``   — cap rows examined (defaults to MAX_WALLETS_PER_RUN).

    Returns a structured summary including a ``cleared_sample`` of wallets
    that were (or would be) un-attributed, with per-wallet reasons.
    """
    try:
        body = request.get_json(silent=True) or {}
        raw_dry = (request.args.get("dry_run") or body.get("dry_run"))
        if raw_dry is None or str(raw_dry).strip() == "":
            dry_run = True  # Safe default — never silently mutate.
        else:
            dry_run = str(raw_dry).strip().lower() in ("1", "true", "yes", "on")

        raw_limit = request.args.get("limit") or body.get("limit")
        limit = None
        if raw_limit is not None and str(raw_limit).strip() != "":
            try:
                limit = max(1, int(raw_limit))
            except (TypeError, ValueError):
                return jsonify({
                    "success": False,
                    "error": "limit must be an integer"
                }), 400

        from goodmarket_attribution_backfill import correct_false_attributions
        summary = correct_false_attributions(dry_run=dry_run, limit=limit)

        try:
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="attribution_correct",
                action_details={
                    "dry_run": dry_run,
                    "limit": limit,
                    "examined": summary.get("examined"),
                    "cleared": summary.get("cleared"),
                    "kept_genuine": summary.get("kept_genuine"),
                },
            )
        except Exception as audit_err:
            logger.warning(f"[gm-attribution-correct] admin audit log skipped: {audit_err}")

        return jsonify(summary), (200 if summary.get("success") else 500)
    except Exception as e:
        logger.error(f"[gm-attribution-correct] admin endpoint error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/users", methods=["GET"])
@admin_required
def get_all_users():
    """Get all users (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        limit = int(request.args.get('limit', 100))
        offset = int(request.args.get('offset', 0))

        # Get users with pagination
        users = safe_supabase_operation(
            lambda: supabase.table('user_data')\
                .select('wallet_address, username, ubi_verified, total_logins, last_login, created_at')\
                .order('created_at', desc=True)\
                .range(offset, offset + limit - 1)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get all users"
        )

        return jsonify({
            "success": True,
            "users": users.data if users.data else [],
            "count": len(users.data) if users.data else 0
        })
    except Exception as e:
        logger.error(f"❌ Get users error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/stats", methods=["GET"])
@admin_required
def get_admin_stats():
    """Get platform statistics (admin only)"""
    try:
        from analytics_service import analytics

        # Get comprehensive platform stats using the correct method
        platform_stats = analytics.get_global_analytics()

        # Extract relevant stats for admin dashboard
        metrics = platform_stats.get("metrics", {})
        stats = {
            "total_users": metrics.get("total_users", 0),
            "verified_users": metrics.get("successful_verifications", 0),
            "total_page_views": platform_stats.get("user_activity", {}).get("total_page_views", 0),
            "verification_rate": platform_stats.get("verification_stats", {}).get("success_rate", "0%"),
            "goodmarket_verified_users": metrics.get("goodmarket_verified_users", 0),
            "pending_verification_users": metrics.get("pending_verification_users", 0),
            "goodmarket_conversion_rate": metrics.get("goodmarket_conversion_rate", "0%")
        }

        return jsonify({
            "success": True,
            "stats": stats
        })
    except Exception as e:
        logger.error(f"❌ Get admin stats error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/referral-stats", methods=["GET"])
@admin_required
def get_admin_referral_stats():
    """Get platform-wide referral statistics (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 500

        referrals_result = supabase.table('referrals').select('*').order('created_at', desc=True).execute()
        referrals = referrals_result.data if referrals_result else []

        rewards_result = supabase.table('referral_rewards_log').select('*').eq('status', 'completed').execute()
        rewards = rewards_result.data if rewards_result else []

        total = len(referrals)
        pending_fv = sum(1 for r in referrals if r.get('status') == 'pending_face_verification')
        pending_disbursed = sum(1 for r in referrals if r.get('status') == 'pending_disbursed')
        completed = sum(1 for r in referrals if r.get('status') == 'completed')
        failed = sum(1 for r in referrals if r.get('status') == 'failed')
        total_g_distributed = sum(float(r.get('reward_amount', 0)) for r in rewards)

        codes_result = supabase.table('referral_codes').select('referral_code, wallet_address, total_earned, created_at').order('total_earned', desc=True).limit(20).execute()
        top_referrers = codes_result.data if codes_result else []

        return jsonify({
            "success": True,
            "summary": {
                "total_referrals": total,
                "pending_face_verification": pending_fv,
                "pending_disbursed": pending_disbursed,
                "completed": completed,
                "failed": failed,
                "total_g_distributed": total_g_distributed
            },
            "recent_referrals": referrals[:50],
            "top_referrers": top_referrers
        })
    except Exception as e:
        logger.error(f"❌ Get admin referral stats error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/referral/disburse-by-code", methods=["POST"])
@admin_required
def admin_disburse_referral_by_code():
    """Admin: trigger disbursement for a specific referral code."""
    try:
        from referral_program.referral_service import referral_service

        data = request.get_json(silent=True) or {}
        referral_code = (data.get("referral_code") or "").strip().upper()
        if not referral_code:
            return jsonify({"success": False, "error": "referral_code is required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 500

        referrals_result = supabase.table("referrals") \
            .select("*") \
            .eq("referral_code", referral_code) \
            .in_("status", ["pending_face_verification", "pending_disbursed", "disbursing"]) \
            .order("created_at", desc=False) \
            .execute()
        referrals = referrals_result.data if referrals_result and referrals_result.data else []
        if not referrals:
            return jsonify({
                "success": False,
                "error": f"No pending referral found for code {referral_code}"
            }), 404

        row = referrals[0]
        referee_wallet = row.get("referee_wallet")
        referrer_wallet = row.get("referrer_wallet")
        if not referee_wallet or not referrer_wallet:
            return jsonify({"success": False, "error": "Referral row missing wallet data"}), 400

        current_status = row.get("status")
        if current_status == "pending_face_verification":
            claim = referral_service.claim_pending_referral_for_disbursement(referee_wallet)
            if not claim.get("claimed"):
                return jsonify({
                    "success": False,
                    "error": "Referral is already being processed. Please refresh and retry."
                }), 409

        disbursement = referral_service.process_referral_disbursement(
            referrer_wallet=referrer_wallet,
            referee_wallet=referee_wallet,
            referral_code=referral_code
        )
        return jsonify({
            "success": bool(disbursement.get("success")),
            "referral_code": referral_code,
            "previous_status": current_status,
            "result": disbursement
        }), (200 if disbursement.get("success") else 202)
    except Exception as e:
        logger.error(f"❌ Admin disburse referral by code error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/set-admin", methods=["POST"])
@admin_required
def set_user_admin_status():
    """Set admin status for a user (admin only)"""
    try:
        from supabase_client import set_admin_status, log_admin_action

        data = request.json
        target_wallet = data.get("wallet_address")
        is_admin_status = data.get("is_admin", False)

        if not target_wallet:
            return jsonify({"success": False, "error": "Wallet address required"}), 400

        admin_wallet = session.get("wallet")

        # Set admin status
        result = set_admin_status(target_wallet, is_admin_status)

        if result.get("success"):
            # Log admin action
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="set_admin_status",
                target_wallet=target_wallet,
                action_details={"is_admin": is_admin_status}
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Set admin status error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/actions-log", methods=["GET"])
@admin_required
def get_admin_actions_log():
    """Get admin actions log (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        limit = int(request.args.get('limit', 50))
        offset = int(request.args.get('offset', 0))

        # Get admin actions with pagination
        actions = safe_supabase_operation(
            lambda: supabase.table('admin_actions_log')\
                .select('*')\
                .order('created_at', desc=True)\
                .range(offset, offset + limit - 1)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get admin actions log"
        )

        return jsonify({
            "success": True,
            "actions": actions.data if actions.data else [],
            "count": len(actions.data) if actions.data else 0
        })
    except Exception as e:
        logger.error(f"❌ Get admin actions log error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/reward-config", methods=["GET"])
@admin_required
def get_reward_config():
    """Get all reward configurations (admin only)"""
    try:
        from reward_config_service import reward_config_service

        result = reward_config_service.get_all_rewards()
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Get reward config error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/reward-config", methods=["POST"])
@admin_required
def update_reward_config():
    """Update reward configuration (admin only)"""
    try:
        from reward_config_service import reward_config_service

        data = request.json
        task_type = data.get('task_type')
        new_amount = float(data.get('reward_amount', 0))
        admin_wallet = session.get('wallet')

        if not task_type or task_type not in ['telegram_task', 'twitter_task']:
            return jsonify({"success": False, "error": "Invalid task type"}), 400

        result = reward_config_service.update_reward_amount(task_type, new_amount, admin_wallet)

        if result.get('success'):
            # Log admin action
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_reward_config",
                action_details={
                    "task_type": task_type,
                    "new_amount": new_amount
                }
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Update reward config error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



@routes.route("/api/admin/quiz-questions", methods=["GET"])
@admin_required
def get_quiz_questions():
    """Get all quiz questions (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        logger.info("📚 Fetching quiz questions from Supabase 'quiz_questions' table...")

        # Get all quiz questions
        questions = safe_supabase_operation(
            lambda: supabase.table('quiz_questions')\
                .select('*')\
                .order('created_at', desc=True)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get quiz questions"
        )

        logger.info(f"✅ Retrieved {len(questions.data) if questions.data else 0} questions from Supabase")
        if questions.data and len(questions.data) > 0:
            logger.info(f"📝 Sample question: ID={questions.data[0].get('question_id')}, Question={questions.data[0].get('question')[:50]}...")

        return jsonify({
            "success": True,
            "questions": questions.data if questions.data else [],
            "count": len(questions.data) if questions.data else 0,
            "data_source": "supabase_quiz_questions_table"
        })
    except Exception as e:
        logger.error(f"��� Get quiz questions error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions", methods=["POST"])
@admin_required
def add_quiz_question():
    """Add new quiz question (admin only)"""
    try:
        data = request.json

        # Validate required fields
        required_fields = ['question_id', 'question', 'answer_a', 'answer_b', 'answer_c', 'answer_d', 'correct']
        for field in required_fields:
            if not data.get(field):
                return jsonify({"success": False, "error": f"Missing required field: {field}"}), 400

        # Validate correct answer is A, B, C, or D
        if data['correct'].upper() not in ['A', 'B', 'C', 'D']:
            return jsonify({"success": False, "error": "Correct answer must be A, B, C, or D"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Check if question_id already exists
        existing = safe_supabase_operation(
            lambda: supabase.table('quiz_questions')\
                .select('question_id')\
                .eq('question_id', data['question_id'])\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="check question_id"
        )

        if existing.data and len(existing.data) > 0:
            return jsonify({"success": False, "error": "Question ID already exists"}), 400

        # Add new question
        from datetime import datetime
        question_data = {
            'question_id': data['question_id'],
            'question': data['question'],
            'answer_a': data['answer_a'],
            'answer_b': data['answer_b'],
            'answer_c': data['answer_c'],
            'answer_d': data['answer_d'],
            'correct': data['correct'].upper(),
            'created_at': datetime.utcnow().isoformat() + 'Z'
        }

        result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions').insert(question_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="add quiz question"
        )

        if result.data:
            # Log admin action
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="add_quiz_question",
                action_details={"question_id": data['question_id']}
            )

            logger.info(f"✅ Quiz question added: {data['question_id']}")
            return jsonify({"success": True, "question": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Failed to add question"}), 500

    except Exception as e:
        logger.error(f"❌ Add quiz question error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions/<question_id>", methods=["PUT"])
@admin_required
def update_quiz_question(question_id):
    """Update quiz question (admin only)"""
    try:
        data = request.json

        # Validate correct answer if provided
        if 'correct' in data and data['correct'].upper() not in ['A', 'B', 'C', 'D']:
            return jsonify({"success": False, "error": "Correct answer must be A, B, C, or D"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Build update data
        update_data = {}
        allowed_fields = ['question', 'answer_a', 'answer_b', 'answer_c', 'answer_d', 'correct']
        for field in allowed_fields:
            if field in data:
                update_data[field] = data[field].upper() if field == 'correct' else data[field]

        if not update_data:
            return jsonify({"success": False, "error": "No valid fields to update"}), 400

        # Update question
        result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions')\
                .update(update_data)\
                .eq('question_id', question_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="update quiz question"
        )

        if result.data:
            # Log admin action
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_quiz_question",
                action_details={"question_id": question_id, "updated_fields": list(update_data.keys())}
            )

            logger.info(f"✅ Quiz question updated: {question_id}")
            return jsonify({"success": True, "question": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Question not found"}), 404

    except Exception as e:
        logger.error(f"❌ Update quiz question error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions/<question_id>", methods=["DELETE"])
@admin_required
def delete_quiz_question(question_id):
    """Delete quiz question (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Delete question
        result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions')\
                .delete()\
                .eq('question_id', question_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete quiz question"
        )

        if result.data:
            # Log admin action
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="delete_quiz_question",
                action_details={"question_id": question_id}
            )

            logger.info(f"✅ Quiz question deleted: {question_id}")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Question not found"}), 404

    except Exception as e:
        logger.error(f"❌ Delete quiz question error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions/delete-all", methods=["DELETE"])
@admin_required
def delete_all_quiz_questions():
    """Delete all quiz questions (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get count of questions before deletion
        count_result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions').select('quiz_id').execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="count quiz questions"
        )

        question_count = len(count_result.data) if count_result.data else 0

        if question_count == 0:
            return jsonify({"success": False, "error": "No questions to delete"}), 400

        # Delete all questions
        result = safe_supabase_operation(
            lambda: supabase.table('quiz_questions').delete().neq('quiz_id', 0).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete all quiz questions"
        )

        # Log admin action
        admin_wallet = session.get("wallet")
        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="delete_all_quiz_questions",
            action_details={"deleted_count": question_count}
        )

        logger.info(f"✅ All quiz questions deleted: {question_count} questions")
        return jsonify({
            "success": True,
            "deleted_count": question_count
        })

    except Exception as e:
        logger.error(f"❌ Delete all quiz questions error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/broadcast-message", methods=["POST"])
@admin_required
def send_broadcast_message():
    """Send broadcast message to all users (admin only)"""
    try:
        data = request.json
        title = data.get('title', '').strip()
        message = data.get('message', '').strip()

        if not title or not message:
            return jsonify({"success": False, "error": "Title and message are required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        admin_wallet = session.get("wallet")

        from datetime import datetime
        broadcast_data = {
            'title': title,
            'message': message,
            'sender_wallet': admin_wallet,
            'is_active': True,
            'created_at': datetime.utcnow().isoformat()
        }

        result = safe_supabase_operation(
            lambda: supabase.table('admin_broadcast_messages').insert(broadcast_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="send broadcast message"
        )

        if result.data:
            # Log admin action
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="send_broadcast_message",
                action_details={"title": title, "message_length": len(message)}
            )

            logger.info(f"✅ Broadcast message sent by admin {admin_wallet[:8]}...")
            return jsonify({
                "success": True,
                "message": "Broadcast message sent successfully!",
                "broadcast_id": result.data[0].get('id')
            })
        else:
            return jsonify({"success": False, "error": "Failed to send broadcast message"}), 500

    except Exception as e:
        logger.error(f"❌ Send broadcast message error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/broadcast-messages", methods=["GET"])
@admin_required
def get_broadcast_messages():
    """Get all broadcast messages (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        limit = int(request.args.get('limit', 50))

        messages = safe_supabase_operation(
            lambda: supabase.table('admin_broadcast_messages')\
                .select('*')\
                .order('created_at', desc=True)\
                .limit(limit)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get broadcast messages"
        )

        return jsonify({
            "success": True,
            "messages": messages.data if messages.data else [],
            "count": len(messages.data) if messages.data else 0
        })

    except Exception as e:
        logger.error(f"❌ Get broadcast messages error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/broadcast-message/<int:broadcast_id>", methods=["DELETE"])
@admin_required
def delete_broadcast_message(broadcast_id):
    """Delete/deactivate broadcast message (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Deactivate instead of delete
        result = safe_supabase_operation(
            lambda: supabase.table('admin_broadcast_messages')\
                .update({'is_active': False})\
                .eq('id', broadcast_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="deactivate broadcast message"
        )

        if result.data:
            admin_wallet = session.get("wallet")
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="delete_broadcast_message",
                action_details={"broadcast_id": broadcast_id}
            )

            logger.info(f"✅ Broadcast message {broadcast_id} deactivated")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Message not found"}), 404

    except Exception as e:
        logger.error(f"❌ Delete broadcast message error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/news-history", methods=["GET"])
@admin_required
def get_news_history():
    """Get all news articles (admin only)"""
    try:
        from news_feed import news_feed_service

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get all news articles
        news = safe_supabase_operation(
            lambda: supabase.table('news_articles')\
                .select('*')\
                .order('created_at', desc=True)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get all news articles"
        )

        return jsonify({
            "success": True,
            "news": news.data if news.data else [],
            "count": len(news.data) if news.data else 0
        })

    except Exception as e:
        logger.error(f"❌ Error getting news history: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ─── Featured Tweets (Community Stories Showcase) ───────────────────────────
import time as _time_mod
_featured_tweets_cache = {"data": None, "expires": 0}
FEATURED_TWEETS_CACHE_TTL = 30  # 30 seconds — fast reflect after admin adds tweet

@routes.route("/api/featured-tweets", methods=["GET"])
def get_featured_tweets():
    """Public — returns active featured tweets with in-memory cache."""
    global _featured_tweets_cache
    now = _time_mod.time()
    if _featured_tweets_cache["data"] is not None and now < _featured_tweets_cache["expires"]:
        return jsonify({"success": True, "tweets": _featured_tweets_cache["data"], "cached": True})
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "tweets": [], "cached": False})
        result = safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases")
                .select("id, tweet_url, tweet_id, label, display_order")
                .eq("is_active", True)
                .order("display_order", desc=False)
                .execute(),
            fallback_result=type("r", (), {"data": []})(),
            operation_name="get featured tweets"
        )
        tweets = result.data or []
        _featured_tweets_cache = {"data": tweets, "expires": now + FEATURED_TWEETS_CACHE_TTL}
        return jsonify({"success": True, "tweets": tweets, "cached": False})
    except Exception as e:
        logger.error(f"❌ get_featured_tweets: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/featured-tweets", methods=["GET"])
@admin_required
def admin_get_featured_tweets():
    """Admin — list all featured tweets."""
    try:
        supabase = get_supabase_client()
        result = safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases")
                .select("*")
                .order("display_order", desc=False)
                .execute(),
            fallback_result=type("r", (), {"data": []})(),
            operation_name="admin get featured tweets"
        )
        return jsonify({"success": True, "tweets": result.data or []})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/featured-tweets", methods=["POST"])
@admin_required
def admin_add_featured_tweet():
    """Admin — add a new tweet link."""
    global _featured_tweets_cache
    try:
        import re as _re
        data = request.get_json() or {}
        tweet_url = (data.get("tweet_url") or "").strip()
        label = (data.get("label") or "").strip()
        display_order = int(data.get("display_order", 0))
        if not tweet_url:
            return jsonify({"success": False, "error": "tweet_url is required"}), 400
        match = _re.search(r"/status/(\d+)", tweet_url)
        tweet_id = match.group(1) if match else None
        supabase = get_supabase_client()
        wallet = session.get("wallet")
        result = safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases").insert({
                "tweet_url": tweet_url,
                "tweet_id": tweet_id,
                "label": label or None,
                "display_order": display_order,
                "is_active": True,
                "added_by": wallet
            }).execute(),
            fallback_result=None,
            operation_name="add featured tweet"
        )
        _featured_tweets_cache["data"] = None
        return jsonify({"success": True, "tweet": result.data[0] if result and result.data else {}})
    except Exception as e:
        logger.error(f"❌ admin_add_featured_tweet: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/featured-tweets/<int:ft_id>", methods=["DELETE"])
@admin_required
def admin_delete_featured_tweet(ft_id):
    """Admin — delete a tweet entry."""
    global _featured_tweets_cache
    try:
        supabase = get_supabase_client()
        safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases").delete().eq("id", ft_id).execute(),
            fallback_result=None,
            operation_name="delete featured tweet"
        )
        _featured_tweets_cache["data"] = None
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/featured-tweets/<int:ft_id>/toggle", methods=["POST"])
@admin_required
def admin_toggle_featured_tweet(ft_id):
    """Admin — toggle active status."""
    global _featured_tweets_cache
    try:
        data = request.get_json() or {}
        is_active = bool(data.get("is_active", True))
        supabase = get_supabase_client()
        safe_supabase_operation(
            lambda: supabase.table("community_tweet_showcases")
                .update({"is_active": is_active}).eq("id", ft_id).execute(),
            fallback_result=None,
            operation_name="toggle featured tweet"
        )
        _featured_tweets_cache["data"] = None
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500
# ────────────────────────────────────────────────────────────────────────────

@routes.route("/api/admin/news/<int:news_id>", methods=["DELETE"])
@admin_required
def delete_news_article(news_id):
    """Delete news article (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Delete news article
        result = safe_supabase_operation(
            lambda: supabase.table('news_articles')\
                .delete()\
                .eq('id', news_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete news article"
        )

        if result.data:
            # Log admin action
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="delete_news_article",
                action_details={"news_id": news_id}
            )

            logger.info(f"✅ News article {news_id} deleted by admin {admin_wallet[:8]}...")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "News article not found"}), 404

    except Exception as e:
        logger.error(f"❌ Error deleting news article: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/publish-news", methods=["POST"])
@admin_required
def publish_news_article():
    """Publish a news article (admin only)"""
    try:
        from news_feed import news_feed_service

        # Get form data
        title = request.form.get('title', '').strip()
        content = request.form.get('content', '').strip()
        category = request.form.get('category', 'announcement')
        priority = request.form.get('priority', 'medium')
        featured = request.form.get('featured') == 'true'
        url = request.form.get('url', '').strip()

        # Validate required fields
        if not title or not content:
            return jsonify({"success": False, "error": "Title and content are required"}), 400

        # Handle image upload if present
        image_url = None
        if 'image' in request.files:
            image_file = request.files['image']
            if image_file and image_file.filename:
                # Upload to ImgBB
                try:
                    import requests
                    import base64

                    imgbb_api_key = os.getenv('IMGBB_API_KEY')
                    if not imgbb_api_key:
                        logger.warning("⚠️ IMGBB_API_KEY not configured - skipping image upload")
                    else:
                        # Reset file pointer to beginning and read image
                        image_file.seek(0)
                        image_data = image_file.read()

                        # Validate image data
                        if not image_data or len(image_data) == 0:
                            logger.error("❌ Image file is empty")
                            return jsonify({"success": False, "error": "Image file is empty"}), 400

                        # Encode to base64
                        encoded_image = base64.b64encode(image_data).decode('utf-8')

                        logger.info(f"📤 Uploading image to ImgBB: {image_file.filename} ({len(image_data)} bytes)")

                        # Upload to ImgBB
                        imgbb_response = requests.post(
                            'https://api.imgbb.com/1/upload',
                            data={
                                'key': imgbb_api_key,
                                'image': encoded_image,
                                'name': f"news_{title[:30]}"
                            },
                            timeout=30
                        )

                        logger.info(f"📥 ImgBB Response: {imgbb_response.status_code}")

                        if imgbb_response.status_code == 200:
                            imgbb_data = imgbb_response.json()
                            if imgbb_data.get('success'):
                                image_url = imgbb_data['data']['url']
                                logger.info(f"✅ Image uploaded to ImgBB: {image_url}")
                            else:
                                error_msg = imgbb_data.get('error', {}).get('message', 'Unknown error')
                                logger.error(f"❌ ImgBB API error: {error_msg}")
                                return jsonify({"success": False, "error": f"Image upload failed: {error_msg}"}), 500
                        else:
                            logger.error(f"❌ ImgBB upload failed: {imgbb_response.status_code} - {imgbb_response.text[:500]}")
                            return jsonify({"success": False, "error": f"Image upload failed with status {imgbb_response.status_code}"}), 500

                except Exception as img_error:
                    logger.error(f"❌ Image upload error: {img_error}")
                    import traceback
                    logger.error(f"Traceback: {traceback.format_exc()}")
                    return jsonify({"success": False, "error": f"Image upload error: {str(img_error)}"}), 500

        # Get admin wallet
        admin_wallet = session.get("wallet")

        # Add news article
        result = news_feed_service.add_news_article(
            title=title,
            content=content,
            category=category,
            priority=priority,
            author=f"Admin ({admin_wallet[:8]}...)",
            featured=featured,
            image_url=image_url,
            url=url if url else None
        )

        if result.get('success'):
            # Log admin action
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="publish_news_article",
                action_details={
                    "title": title,
                    "category": category,
                    "featured": featured,
                    "has_image": bool(image_url)
                }
            )

            logger.info(f"✅ News article published: {title}")
            return jsonify({
                "success": True,
                "message": "News article published successfully!",
                "article": result.get('article')
            })
        else:
            return jsonify({
                "success": False,
                "error": result.get('error', 'Failed to publish article')
            }), 500

    except Exception as e:
        logger.error(f"❌ Publish news article error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/learn-earn-sell-date", methods=["GET"])
@admin_required
def get_learn_earn_sell_date():
    """Get the current achievement card sell start date"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('custom_message')\
                .eq('feature_name', 'learn_earn_sell_date')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get learn earn sell date"
        )

        sell_date = None
        if result.data and len(result.data) > 0:
            sell_date = result.data[0].get('custom_message')

        return jsonify({
            "success": True,
            "sell_date": sell_date or "2026-05-10"
        })
    except Exception as e:
        logger.error(f"❌ Error getting sell date: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/learn-earn-sell-date", methods=["POST"])
@admin_required
def set_learn_earn_sell_date():
    """Update the achievement card sell start date"""
    try:
        data = request.json
        sell_date = data.get('sell_date', '').strip()

        if not sell_date:
            return jsonify({"success": False, "error": "sell_date is required"}), 400

        from datetime import datetime
        try:
            datetime.strptime(sell_date, '%Y-%m-%d')
        except ValueError:
            return jsonify({"success": False, "error": "Invalid date format. Use YYYY-MM-DD"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('id')\
                .eq('feature_name', 'learn_earn_sell_date')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="check existing sell date"
        )

        if existing.data and len(existing.data) > 0:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .update({'custom_message': sell_date})\
                    .eq('feature_name', 'learn_earn_sell_date')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="update sell date"
            )
        else:
            safe_supabase_operation(
                lambda: supabase.table('maintenance_settings').insert({
                    'feature_name': 'learn_earn_sell_date',
                    'is_maintenance': False,
                    'custom_message': sell_date
                }).execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="insert sell date"
            )

        admin_wallet = session.get('wallet', 'unknown')
        logger.info(f"✅ Achievement card sell date updated to {sell_date} by admin {admin_wallet[:8]}...")

        return jsonify({
            "success": True,
            "sell_date": sell_date,
            "message": f"Sell date updated to {sell_date}"
        })
    except Exception as e:
        logger.error(f"❌ Error setting sell date: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/maintenance/learn-earn", methods=["GET"])
@admin_required
def get_learn_earn_maintenance():
    """Get Learn & Earn maintenance status"""
    try:
        from maintenance_service import maintenance_service

        status = maintenance_service.get_maintenance_status('learn_earn')
        return jsonify(status)
    except Exception as e:
        logger.error(f"❌ Error getting maintenance status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/maintenance/learn-earn", methods=["POST"])
@admin_required
def set_learn_earn_maintenance():
    """Set Learn & Earn maintenance status"""
    try:
        from maintenance_service import maintenance_service

        data = request.json
        is_maintenance = data.get('is_maintenance', False)
        message = data.get('message', '')
        admin_wallet = session.get('wallet')

        if is_maintenance and not message:
            return jsonify({
                "success": False,
                "error": "Custom message is required when enabling maintenance mode"
            }), 400

        result = maintenance_service.set_maintenance_status(
            'learn_earn',
            is_maintenance,
            message,
            admin_wallet
        )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error setting maintenance status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/maintenance/minigames", methods=["GET"])
@admin_required
def get_minigames_maintenance():
    """Get Minigames maintenance status"""
    try:
        from maintenance_service import maintenance_service

        status = maintenance_service.get_maintenance_status('minigames')
        return jsonify(status)
    except Exception as e:
        logger.error(f"❌ Error getting minigames maintenance status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/maintenance/minigames", methods=["POST"])
@admin_required
def set_minigames_maintenance():
    """Set Minigames maintenance status"""
    try:
        from maintenance_service import maintenance_service

        data = request.json
        is_maintenance = data.get('is_maintenance', False)
        message = data.get('message', '')
        admin_wallet = session.get('wallet')

        if is_maintenance and not message:
            return jsonify({
                "success": False,
                "error": "Custom message is required when enabling maintenance mode"
            }), 400

        result = maintenance_service.set_maintenance_status(
            'minigames',
            is_maintenance,
            message,
            admin_wallet
        )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error setting minigames maintenance status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-settings", methods=["GET"])
@admin_required
def get_quiz_settings():
    """Get current quiz settings"""
    try:
        from learn_and_earn.learn_and_earn import quiz_manager

        settings = quiz_manager.get_quiz_settings()
        return jsonify({
            "success": True,
            "settings": settings
        })
    except Exception as e:
        logger.error(f"❌ Error getting quiz settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/community-stories-settings", methods=["GET"])
@admin_required
def get_community_stories_settings():
    """Get Community Stories settings (admin only)"""
    try:
        from community_stories.community_stories_service import community_stories_service

        config = community_stories_service.get_config()

        # Get message from database
        supabase = get_supabase_client()
        message = None

        if supabase:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .select('custom_message')\
                    .eq('feature_name', 'community_stories_message')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="get community stories message"
            )

            if result.data and len(result.data) > 0:
                message = result.data[0].get('custom_message')

        return jsonify({
            "success": True,
            "settings": {
                "low_reward": config['LOW_REWARD'],
                "high_reward": config['HIGH_REWARD'],
                "required_mentions": config['REQUIRED_MENTIONS'],
                "window_start_day": config['WINDOW_START_DAY'],
                "window_end_day": config['WINDOW_END_DAY'],
                "message": message or """💰 Earn G$ by sharing our story:
2,000 G$ - Text post on Twitter/X
5,000 G$ - Video post (min. 30 seconds)

📋 Requirements:
Must use hashtags: @gooddollarorg @GoodDollarTeam
Post must be public
Original content only

📅 Participation Schedule:
Opens: 26th of each month at 12:00 AM UTC
Closes: 30th of each month at 11:59 PM UTC
Duration: 5 days only each month
After reward: Blocked until next 26th

⚠️ Late submissions after 30th are NOT accepted!"""
            }
        })
    except Exception as e:
        logger.error(f"❌ Error getting Community Stories settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/community-stories-settings", methods=["POST"])
@admin_required
def update_community_stories_settings():
    """Update Community Stories settings (admin only)"""
    try:
        data = request.json
        low_reward = data.get('low_reward')
        high_reward = data.get('high_reward')
        required_mentions = data.get('required_mentions')
        window_start_day = data.get('window_start_day')
        window_end_day = data.get('window_end_day')
        message = data.get('message', '').strip()

        if not all([low_reward, high_reward, required_mentions, window_start_day, window_end_day]):
            return jsonify({"success": False, "error": "All fields are required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Store settings in database using custom_message field for JSON data
        settings_json = json.dumps({
            'low_reward': float(low_reward),
            'high_reward': float(high_reward),
            'required_mentions': str(required_mentions),
            'window_start_day': int(window_start_day),
            'window_end_day': int(window_end_day)
        })

        settings_data = {
            'feature_name': 'community_stories_config',
            'is_maintenance': False,  # Use boolean field properly
            'custom_message': settings_json  # Store JSON in text field
        }

        # Check if exists
        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('id')\
                .eq('feature_name', 'community_stories_config')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="check community stories config"
        )

        if existing.data and len(existing.data) > 0:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .update(settings_data)\
                    .eq('feature_name', 'community_stories_config')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="update community stories config"
            )
        else:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings').insert(settings_data).execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="insert community stories config"
            )

        # Store message separately
        if message:
            message_data = {
                'feature_name': 'community_stories_message',
                'is_maintenance': False,  # Use boolean field properly
                'custom_message': message  # Store message in text field
            }

            existing_msg = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .select('id')\
                    .eq('feature_name', 'community_stories_message')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="check community stories message"
            )

            if existing_msg.data and len(existing_msg.data) > 0:
                safe_supabase_operation(
                    lambda: supabase.table('maintenance_settings')\
                        .update(message_data)\
                        .eq('feature_name', 'community_stories_message')\
                        .execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="update community stories message"
                )
            else:
                safe_supabase_operation(
                    lambda: supabase.table('maintenance_settings').insert(message_data).execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="insert community stories message"
                )

        if result.data:
            # Log admin action
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_community_stories_settings",
                action_details={
                    "low_reward": low_reward,
                    "high_reward": high_reward,
                    "window_start_day": window_start_day,
                    "window_end_day": window_end_day,
                    "message_updated": bool(message)
                }
            )

            logger.info(f"✅ Community Stories settings updated by admin {admin_wallet[:8]}...")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to update settings"}), 500

    except Exception as e:
        logger.error(f"❌ Error updating Community Stories settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/insufficient-balance-message", methods=["GET"])
@admin_required
def get_insufficient_balance_message():
    """Get current insufficient balance error message"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get message from maintenance_settings table
        result = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('custom_message')\
                .eq('feature_name', 'learn_earn_insufficient_balance')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get insufficient balance message"
        )

        message = None
        if result.data and len(result.data) > 0:
            message = result.data[0].get('custom_message')

        return jsonify({
            "success": True,
            "message": message
        })
    except Exception as e:
        logger.error(f"❌ Error getting insufficient balance message: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/insufficient-balance-message", methods=["POST"])
@admin_required
def update_insufficient_balance_message():
    """Update insufficient balance error message"""
    try:
        data = request.json
        message = data.get('message', '').strip()

        if not message:
            return jsonify({
                "success": False,
                "error": "Message is required"
            }), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Check if record exists
        existing = safe_supabase_operation(
            lambda: supabase.table('maintenance_settings')\
                .select('id')\
                .eq('feature_name', 'learn_earn_insufficient_balance')\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="check existing message"
        )

        if existing.data and len(existing.data) > 0:
            # Update existing record
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')\
                    .update({'custom_message': message})\
                    .eq('feature_name', 'learn_earn_insufficient_balance')\
                    .execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="update insufficient balance message"
            )
        else:
            # Insert new record
            from datetime import datetime
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings').insert({
                    'feature_name': 'learn_earn_insufficient_balance',
                    'is_maintenance': False,
                    'custom_message': message,
                    'created_at': datetime.utcnow().isoformat()
                }).execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name="insert insufficient balance message"
            )

        if result.data:
            # Log admin action
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_insufficient_balance_message",
                action_details={"message_length": len(message)}
            )

            logger.info(f"✅ Insufficient balance message updated by admin {admin_wallet[:8]}...")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to update message"}), 500

    except Exception as e:
        logger.error(f"❌ Error updating insufficient balance message: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-settings", methods=["POST"])
@admin_required
def update_quiz_settings():
    """Update quiz settings"""
    try:
        from learn_and_earn.learn_and_earn import quiz_manager

        data = request.json
        questions_per_quiz = data.get('questions_per_quiz')
        time_per_question = data.get('time_per_question')
        max_reward_per_quiz = data.get('max_reward_per_quiz')

        # Validate inputs
        if questions_per_quiz is not None and (questions_per_quiz < 5 or questions_per_quiz > 30):
            return jsonify({
                "success": False,
                "error": "Questions per quiz must be between 5 and 30"
            }), 400

        if time_per_question is not None and (time_per_question < 10 or time_per_question > 60):
            return jsonify({
                "success": False,
                "error": "Time per question must be between 10 and 60 seconds"
            }), 400

        if max_reward_per_quiz is not None and (max_reward_per_quiz < 500 or max_reward_per_quiz > 10000):
            return jsonify({
                "success": False,
                "error": "Max reward must be between 500 and 10,000 G$"
            }), 400

        result = quiz_manager.update_quiz_settings(
            questions_per_quiz=questions_per_quiz,
            time_per_question=time_per_question,
            max_reward_per_quiz=max_reward_per_quiz
        )

        if result.get('success'):
            # Log admin action
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_quiz_settings",
                action_details={
                    "questions_per_quiz": questions_per_quiz,
                    "time_per_question": time_per_question,
                    "max_reward_per_quiz": max_reward_per_quiz
                }
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error updating quiz settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/referral/my-code", methods=["GET"])
@auth_required
def get_my_referral_code():
    """Get or generate the current user's referral code.
    Checks user_data.my_referral_code first for a fast single-row lookup,
    then falls back to get_or_create_referral_code if not yet populated.
    """
    try:
        wallet = session.get('wallet')
        from referral_program.referral_service import referral_service, BASE_URL

        # Fast path: check user_data directly
        supabase = supabase_logger.client if supabase_logger and supabase_logger.enabled else None
        if supabase:
            ud = supabase.table('user_data') \
                .select('my_referral_code') \
                .ilike('wallet_address', wallet) \
                .limit(1) \
                .execute()
            if ud.data and ud.data[0].get('my_referral_code'):
                code = ud.data[0]['my_referral_code']
                return jsonify({
                    "success": True,
                    "referral_code": code,
                    "referral_link": f"{BASE_URL}/?ref={code}",
                    "source": "user_data"
                })

        # Slow path: generate/fetch from referral_codes table (also syncs back to user_data)
        result = referral_service.get_or_create_referral_code(wallet)
        if result.get('success'):
            result['source'] = 'referral_codes'
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting referral code: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/referral/stats", methods=["GET"])
@auth_required
def get_referral_stats():
    """Get referral program statistics for the current user."""
    try:
        wallet = session.get('wallet')
        from referral_program.referral_service import referral_service
        result = referral_service.get_referral_stats(wallet)
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error getting referral stats: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/referral/process-pending", methods=["POST"])
@admin_required
def process_pending_referral_rewards():
    """Admin: attempt to disburse all pending referral rewards."""
    try:
        from referral_program.referral_service import referral_service
        result = referral_service.process_pending_disbursements()
        return jsonify(result)
    except Exception as e:
        logger.error(f"Error processing pending referral rewards: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/referral/key-balance", methods=["GET"])
@admin_required
def get_referral_key_balance():
    """Admin: check REFERRAL_KEY wallet balance and pending disbursement queue."""
    try:
        from referral_program.blockchain import referral_blockchain_service
        from referral_program.referral_service import referral_service
        
        # Check REFERRAL_KEY balance
        balance_result = referral_blockchain_service.get_referral_wallet_balance()
        
        # Count pending disbursements waiting for balance
        supabase = get_supabase_client()
        pending_count = 0
        total_pending_amount = 0.0
        
        if supabase:
            try:
                pending_rewards = safe_supabase_operation(
                    lambda: supabase.table('referral_rewards_log')
                        .select('reward_amount')
                        .eq('status', 'pending_disbursed')
                        .execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="get pending disbursed count"
                )
                if pending_rewards and pending_rewards.data:
                    pending_count = len(pending_rewards.data)
                    total_pending_amount = sum(float(r.get('reward_amount', 0)) for r in pending_rewards.data)
            except Exception as e:
                logger.warning(f"Could not fetch pending disbursements count: {e}")
        
        return jsonify({
            "success": balance_result.get("success", False),
            "balance_g": balance_result.get("balance", 0),
            "balance_wei": balance_result.get("balance_wei", 0),
            "wallet": balance_result.get("wallet", "N/A"),
            "pending_disbursements_count": pending_count,
            "total_pending_amount_g": total_pending_amount,
            "can_process": (balance_result.get("balance", 0) >= total_pending_amount) if balance_result.get("success") else False,
            "error": balance_result.get("error")
        })
    except Exception as e:
        logger.error(f"Error checking referral key balance: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/referral/check/<referral_code>", methods=["GET"])
def check_referral_status(referral_code):
    """Check referral code status and history (for debugging)"""
    try:
        from referral_program.referral_service import referral_service

        # Validate code
        validation = referral_service.validate_referral_code(referral_code)

        # Get referrals using this code
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        referrals = safe_supabase_operation(
            lambda: supabase.table('referrals').select('*').eq('referral_code', referral_code).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get referrals by code"
        )

        rewards = safe_supabase_operation(
            lambda: supabase.table('referral_rewards_log').select('*').eq('referral_code', referral_code).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get rewards by code"
        )

        return jsonify({
            "success": True,
            "referral_code": referral_code,
            "validation": validation,
            "referrals": referrals.data if referrals.data else [],
            "rewards": rewards.data if rewards.data else [],
            "total_referrals": len(referrals.data) if referrals.data else 0,
            "total_rewards": len(rewards.data) if rewards.data else 0
        })
    except Exception as e:
        logger.error(f"❌ Error checking referral status: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/daily-tasks/pending", methods=["GET"])
@admin_required
def get_pending_daily_tasks():
    """Get pending daily task submissions (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get pending Telegram tasks
        telegram_pending = safe_supabase_operation(
            lambda: supabase.table('telegram_task_log')\
                .select('*')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending telegram tasks"
        )

        # Get pending Twitter tasks
        twitter_pending = safe_supabase_operation(
            lambda: supabase.table('twitter_task_log')\
                .select('*')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending twitter tasks"
        )

        # Get pending Telegram tasks
        telegram_pending = safe_supabase_operation(
            lambda: supabase.table('telegram_task_log')\
                .select('*')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending telegram tasks"
        )

        # Get pending Twitter tasks
        twitter_pending = safe_supabase_operation(
            lambda: supabase.table('twitter_task_log')\
                .select('*')\
                .eq('status', 'pending')\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending twitter tasks"
        )

        telegram_tasks = []
        if telegram_pending.data:
            for task in telegram_pending.data:
                telegram_tasks.append({
                    'id': task.get('id'),
                    'wallet_address': task.get('wallet_address'),
                    'url': task.get('telegram_url'),
                    'reward_amount': task.get('reward_amount'),
                    'created_at': task.get('created_at'),
                    'platform': 'telegram'
                })

        twitter_tasks = []
        if twitter_pending.data:
            for task in twitter_pending.data:
                twitter_tasks.append({
                    'id': task.get('id'),
                    'wallet_address': task.get('wallet_address'),
                    'url': task.get('twitter_url'),
                    'reward_amount': task.get('reward_amount'),
                    'created_at': task.get('created_at'),
                    'platform': 'twitter'
                })

        return jsonify({
            "success": True,
            "telegram_tasks": telegram_tasks,
            "twitter_tasks": twitter_tasks,
            "total_pending": len(telegram_tasks) + len(twitter_tasks)
        })

    except Exception as e:
        logger.error(f"❌ Error getting pending tasks: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/daily-tasks/approve", methods=["POST"])
@admin_required
def approve_daily_task():
    """Approve a daily task submission (admin only)"""
    try:
        data = request.json
        submission_id = data.get('submission_id')
        platform = data.get('platform')  # 'telegram' or 'twitter' or 'facebook'
        admin_wallet = session.get('wallet')

        if not submission_id or not platform:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = None
            if platform == 'telegram':
                from telegram_task.telegram_task import telegram_task_service
                result = loop.run_until_complete(
                    telegram_task_service.approve_submission(submission_id, admin_wallet)
                )
            elif platform == 'twitter':
                from twitter_task.twitter_task import twitter_task_service
                result = loop.run_until_complete(
                    twitter_task_service.approve_submission(submission_id, admin_wallet)
                )
            else:
                return jsonify({"success": False, "error": "Invalid platform"}), 400

            # Log admin action
            if result and result.get('success'):
                log_admin_action(
                    admin_wallet=admin_wallet,
                    action_type=f"approve_{platform}_task",
                    action_details={"submission_id": submission_id}
                )

            return jsonify(result) if result else jsonify({"success": False, "error": "Failed to process approval"}), 500
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Error approving task: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/daily-tasks/reject", methods=["POST"])
@admin_required
def reject_daily_task():
    """Reject a daily task submission (admin only)"""
    try:
        data = request.json
        submission_id = data.get('submission_id')
        platform = data.get('platform')  # 'telegram' or 'twitter' or 'facebook'
        reason = data.get('reason', '')
        admin_wallet = session.get('wallet')

        if not submission_id or not platform:
            return jsonify({"success": False, "error": "Missing required fields"}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            result = None
            if platform == 'telegram':
                from telegram_task.telegram_task import telegram_task_service
                result = loop.run_until_complete(
                    telegram_task_service.reject_submission(submission_id, admin_wallet, reason)
                )
            elif platform == 'twitter':
                from twitter_task.twitter_task import twitter_task_service
                result = loop.run_until_complete(
                    twitter_task_service.reject_submission(submission_id, admin_wallet, reason)
                )
            else:
                return jsonify({"success": False, "error": "Invalid platform"}), 400

            # Log admin action
            if result and result.get('success'):
                log_admin_action(
                    admin_wallet=admin_wallet,
                    action_type=f"reject_{platform}_task",
                    action_details={"submission_id": submission_id, "reason": reason}
                )

            return jsonify(result) if result else jsonify({"success": False, "error": "Failed to process rejection"}), 500
        finally:
            loop.close()

    except Exception as e:
        logger.error(f"❌ Error rejecting task: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

# DEPRECATED: the threading.Thread + in-memory _bulk_jobs pattern below
# does NOT survive Vercel's serverless function lifecycle — the lambda is
# frozen/killed shortly after the response is returned, so the thread is
# terminated mid-batch and the polling endpoint lands on a different
# instance with an empty _bulk_jobs dict. The admin UI now drives bulk
# approve/reject from the browser using the per-item endpoints
# (/api/admin/daily-tasks/approve and /reject), which works reliably on
# serverless because each call is its own short-lived request.
#
# These endpoints are kept for backwards compat in case external callers
# still hit them, but the admin dashboard no longer uses them. Do NOT
# build new functionality on top of this background-thread pattern; it
# will silently lose work in production.
_bulk_jobs = {}

def _run_bulk_approve_job(job_id, tasks, delay_seconds, admin_wallet):
    """[DEPRECATED] Background thread worker for bulk approve daily tasks.

    See the note above _bulk_jobs — the admin dashboard now drives bulk
    operations from the browser instead of using this background job.
    """
    import time as time_module
    import asyncio

    job = _bulk_jobs[job_id]
    job['status'] = 'running'

    for index, task in enumerate(tasks):
        submission_id = task.get('submission_id')
        platform = task.get('platform')

        if not submission_id or platform not in ['twitter', 'telegram']:
            job['results'].append({
                'submission_id': submission_id,
                'platform': platform,
                'success': False,
                'error': 'Invalid task data'
            })
            job['processed'] += 1
            continue

        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                if platform == 'telegram':
                    from telegram_task.telegram_task import telegram_task_service
                    result = loop.run_until_complete(telegram_task_service.approve_submission(submission_id, admin_wallet))
                elif platform == 'twitter':
                    from twitter_task.twitter_task import twitter_task_service
                    result = loop.run_until_complete(twitter_task_service.approve_submission(submission_id, admin_wallet))
                else:
                    result = None
            finally:
                loop.close()

            if result and result.get('success'):
                log_admin_action(admin_wallet=admin_wallet, action_type=f"approve_{platform}_task", action_details={"submission_id": submission_id})

            job['results'].append({
                'submission_id': submission_id,
                'platform': platform,
                'success': result.get('success', False) if result else False,
                'tx_hash': result.get('tx_hash') if result else None,
                'error': result.get('error') if result else 'No result'
            })

        except Exception as e:
            logger.error(f"❌ Error bulk approving task {submission_id}: {e}")
            job['results'].append({
                'submission_id': submission_id,
                'platform': platform,
                'success': False,
                'error': str(e)
            })

        job['processed'] += 1

        if index < len(tasks) - 1:
            time_module.sleep(delay_seconds)

    job['status'] = 'done'
    succeeded = [r for r in job['results'] if r['success']]
    failed = [r for r in job['results'] if not r['success']]
    job['succeeded'] = len(succeeded)
    job['failed'] = len(failed)
    logger.info(f"📊 Bulk daily task approve [{job_id}]: {len(succeeded)} succeeded, {len(failed)} failed")


@routes.route("/api/admin/daily-tasks/bulk-approve", methods=["POST"])
@admin_required
def bulk_approve_daily_tasks():
    """Start bulk approve daily task submissions as a background job (admin only)"""
    try:
        import uuid, threading
        data = request.json
        tasks = data.get('tasks', [])
        delay_seconds = int(data.get('delay_seconds', 4))
        admin_wallet = session.get('wallet')

        if not tasks:
            return jsonify({"success": False, "error": "No tasks provided"}), 400

        if delay_seconds < 2:
            delay_seconds = 2
        if delay_seconds > 30:
            delay_seconds = 30

        job_id = str(uuid.uuid4())
        _bulk_jobs[job_id] = {
            'status': 'pending',
            'total': len(tasks),
            'processed': 0,
            'succeeded': 0,
            'failed': 0,
            'results': []
        }

        logger.info(f"📦 Admin {admin_wallet[:8]}... starting bulk approve job {job_id} for {len(tasks)} tasks")

        t = threading.Thread(
            target=_run_bulk_approve_job,
            args=(job_id, tasks, delay_seconds, admin_wallet),
            daemon=True
        )
        t.start()

        return jsonify({"success": True, "job_id": job_id, "total": len(tasks)})

    except Exception as e:
        logger.error(f"❌ Error starting bulk daily task approve: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/daily-tasks/bulk-status/<job_id>", methods=["GET"])
@admin_required
def bulk_approve_status(job_id):
    """Poll status of a background bulk approve job"""
    job = _bulk_jobs.get(job_id)
    if not job:
        return jsonify({"success": False, "error": "Job not found"}), 404
    return jsonify({
        "success": True,
        "status": job['status'],
        "total": job['total'],
        "processed": job['processed'],
        "succeeded": job['succeeded'],
        "failed": job['failed'],
        "results": job['results']
    })

@routes.route("/api/admin/daily-tasks/bulk-reject", methods=["POST"])
@admin_required
def bulk_reject_daily_tasks():
    """Bulk reject daily task submissions (admin only)"""
    try:
        data = request.json
        tasks = data.get('tasks', [])  # list of {submission_id, platform}
        reason = data.get('reason', '')
        admin_wallet = session.get('wallet')

        if not tasks:
            return jsonify({"success": False, "error": "No tasks provided"}), 400

        logger.info(f"📦 Admin {admin_wallet[:8]}... bulk rejecting {len(tasks)} daily tasks")

        results = []

        for task in tasks:
            submission_id = task.get('submission_id')
            platform = task.get('platform')

            if not submission_id or platform not in ['twitter', 'telegram']:
                results.append({'submission_id': submission_id, 'platform': platform, 'success': False, 'error': 'Invalid task data'})
                continue

            try:
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    if platform == 'telegram':
                        from telegram_task.telegram_task import telegram_task_service
                        result = loop.run_until_complete(telegram_task_service.reject_submission(submission_id, admin_wallet, reason))
                    elif platform == 'twitter':
                        from twitter_task.twitter_task import twitter_task_service
                        result = loop.run_until_complete(twitter_task_service.reject_submission(submission_id, admin_wallet, reason))
                finally:
                    loop.close()

                if result and result.get('success'):
                    log_admin_action(admin_wallet=admin_wallet, action_type=f"reject_{platform}_task", action_details={"submission_id": submission_id, "reason": reason})

                results.append({
                    'submission_id': submission_id,
                    'platform': platform,
                    'success': result.get('success', False) if result else False,
                    'error': result.get('error') if result else 'No result'
                })

            except Exception as e:
                logger.error(f"❌ Error bulk rejecting task {submission_id}: {e}")
                results.append({'submission_id': submission_id, 'platform': platform, 'success': False, 'error': str(e)})

        succeeded = [r for r in results if r['success']]
        failed = [r for r in results if not r['success']]

        logger.info(f"📊 Bulk daily task reject: {len(succeeded)} succeeded, {len(failed)} failed")

        return jsonify({
            'success': True,
            'total': len(tasks),
            'succeeded': len(succeeded),
            'failed': len(failed),
            'results': results
        })

    except Exception as e:
        logger.error(f"❌ Error in bulk daily task reject: {e}")
        import traceback
        logger.error(f"🔍 Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/quiz-questions/upload", methods=["POST"])
@admin_required
def upload_quiz_questions():
    """Upload quiz questions from TXT file (admin only)"""
    try:
        if 'file' not in request.files:
            return jsonify({"success": False, "error": "No file uploaded"}), 400

        file = request.files['file']

        if file.filename == '':
            return jsonify({"success": False, "error": "No file selected"}), 400

        if not file.filename.endswith('.txt'):
            return jsonify({"success": False, "error": "File must be .txt format"}), 400

        # Read file content
        content = file.read().decode('utf-8')

        # Parse questions from TXT content
        questions = []
        current_question = {}
        parse_errors = []
        line_number = 0

        for line in content.split('\n'):
            line_number += 1
            line = line.strip()

            if not line:
                # Empty line - end of question
                if current_question:
                    # Check if all required fields are present
                    required_fields = ['question_id', 'question', 'answer_a', 'answer_b', 'answer_c', 'answer_d', 'correct']
                    missing_fields = [f for f in required_fields if f not in current_question]

                    if missing_fields:
                        parse_errors.append(f"Question at line ~{line_number}: Missing fields: {', '.join(missing_fields)}")
                    else:
                        questions.append(current_question)
                    current_question = {}
                continue

            if line.startswith('QUESTION_ID:'):
                current_question['question_id'] = line.replace('QUESTION_ID:', '').strip()
            elif line.startswith('QUESTION:'):
                current_question['question'] = line.replace('QUESTION:', '').strip()
            elif line.startswith('A)') or line.startswith('A:'):
                current_question['answer_a'] = line.replace('A)', '').replace('A:', '').strip()
            elif line.startswith('B)') or line.startswith('B:'):
                current_question['answer_b'] = line.replace('B)', '').replace('B:', '').strip()
            elif line.startswith('C)') or line.startswith('C:'):
                current_question['answer_c'] = line.replace('C)', '').replace('C:', '').strip()
            elif line.startswith('D)') or line.startswith('D:'):
                current_question['answer_d'] = line.replace('D)', '').replace('D:', '').strip()
            elif line.startswith('CORRECT:'):
                correct = line.replace('CORRECT:', '').strip().upper()
                if correct in ['A', 'B', 'C', 'D']:
                    current_question['correct'] = correct
                else:
                    parse_errors.append(f"Line {line_number}: Invalid correct answer '{correct}'. Must be A, B, C, or D")

        # Add last question if exists
        if current_question:
            required_fields = ['question_id', 'question', 'answer_a', 'answer_b', 'answer_c', 'answer_d', 'correct']
            missing_fields = [f for f in required_fields if f not in current_question]

            if missing_fields:
                parse_errors.append(f"Last question: Missing fields: {', '.join(missing_fields)}")
            else:
                questions.append(current_question)

        if not questions:
            example_format = """
Expected format (each question must have ALL fields):

QUESTION_ID: Q001
QUESTION: What is GoodDollar?
A: A cryptocurrency for UBI
B: A bank
C: A credit card
D: A website
CORRECT: A

(Empty line between questions)

QUESTION_ID: Q002
QUESTION: How often can you claim UBI?
A: Monthly
B: Daily
C: Yearly
D: Once
CORRECT: B
"""
            error_msg = "No valid questions found in file."
            if parse_errors:
                error_msg += f" Errors found: {'; '.join(parse_errors[:3])}"
            error_msg += f" Please check file format. {example_format}"

            return jsonify({
                "success": False,
                "error": error_msg,
                "parse_errors": parse_errors,
                "example_format": example_format
            }), 400

        # Insert questions into database
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        added_count = 0
        skipped_count = 0
        error_count = 0
        error_details = []

        admin_wallet = session.get("wallet")

        for q in questions:
            try:
                # Check if question_id already exists
                existing = safe_supabase_operation(
                    lambda: supabase.table('quiz_questions')\
                        .select('question_id')\
                        .eq('question_id', q['question_id'])\
                        .execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="check question exists"
                )

                if existing.data and len(existing.data) > 0:
                    skipped_count += 1
                    logger.info(f"⚠️ Skipped duplicate question: {q['question_id']}")
                    continue

                # Add created_at timestamp
                from datetime import datetime
                q['created_at'] = datetime.utcnow().isoformat() + 'Z'

                # Insert question
                result = safe_supabase_operation(
                    lambda: supabase.table('quiz_questions').insert(q).execute(),
                    fallback_result=type('obj', (object,), {'data': []})(),
                    operation_name="insert question from file"
                )

                if result.data:
                    added_count += 1
                    logger.info(f"✅ Added question from file: {q['question_id']}")
                else:
                    error_count += 1
                    error_details.append(f"Failed to add {q['question_id']}")

            except Exception as e:
                error_count += 1
                error_details.append(f"{q.get('question_id', 'unknown')}: {str(e)}")
                logger.error(f"❌ Error adding question {q.get('question_id', 'unknown')}: {e}")

        # Log admin action
        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="upload_quiz_questions",
            action_details={
                "total_questions": len(questions),
                "added": added_count,
                "skipped": skipped_count,
                "errors": error_count
            }
        )

        logger.info(f"✅ Quiz upload complete: {added_count} added, {skipped_count} skipped, {error_count} errors")

        return jsonify({
            "success": True,
            "total": len(questions),
            "added": added_count,
            "skipped": skipped_count,
            "errors": error_count,
            "error_details": error_details[:10]  # Limit to first 10 errors
        })

    except Exception as e:
        logger.error(f"❌ Upload quiz questions error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/module-links", methods=["GET"])
@admin_required
def get_module_links():
    """Get all module links (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get all module links
        links = safe_supabase_operation(
            lambda: supabase.table('learn_earn_module_links')\
                .select('*')\
                .order('display_order', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get module links"
        )

        return jsonify({
            "success": True,
            "links": links.data if links.data else [],
            "count": len(links.data) if links.data else 0
        })
    except Exception as e:
        logger.error(f"❌ Get module links error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/module-links", methods=["POST"])
@admin_required
def add_module_link():
    """Add new module link (admin only) - supports auto-scraping from URL"""
    try:
        data = request.json
        title = data.get('title', '').strip()
        url = data.get('url', '').strip()
        description = data.get('description', '').strip()
        content = data.get('content', '').strip()
        reading_time_minutes = int(data.get('reading_time_minutes', 5))
        display_order = int(data.get('display_order', 1))

        if not title:
            return jsonify({"success": False, "error": "Title is required"}), 400

        # Auto-scrape content from URL if no content provided - ALWAYS ENABLED
        scrape_warning = None
        if url and not content:
            logger.info(f"🔍 🤖 AUTO-SCRAPING ENABLED - Fetching content from URL: {url}")
            try:
                import requests
                import json as json_lib
                from bs4 import BeautifulSoup

                scraped_html = ""
                is_medium = 'medium.com' in url

                # --- Medium-specific: use hidden JSON API ---
                if is_medium:
                    logger.info(f"📰 Medium URL detected — using Medium JSON API")
                    # Strip query params and append ?format=json
                    base_url = url.split('?')[0].rstrip('/')
                    json_url = base_url + '?format=json'
                    medium_headers = {
                        'User-Agent': 'Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)',
                        'Accept': 'application/json, text/plain, */*',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Referer': 'https://medium.com/',
                    }
                    try:
                        json_resp = requests.get(json_url, timeout=15, headers=medium_headers, allow_redirects=True)
                        json_resp.raise_for_status()
                        # Medium prepends '])}while(1);</x>' as XSSI protection — strip it
                        raw = json_resp.text
                        json_start = raw.find('{')
                        if json_start != -1:
                            data = json_lib.loads(raw[json_start:])
                            # Navigate to paragraphs inside the post payload
                            payload = data.get('payload', {})
                            post_value = payload.get('value', {})
                            paragraphs = post_value.get('content', {}).get('bodyModel', {}).get('paragraphs', [])
                            if not paragraphs:
                                # Try alternative path
                                post_map = payload.get('references', {}).get('Post', {})
                                if post_map:
                                    first_post = list(post_map.values())[0]
                                    paragraphs = first_post.get('content', {}).get('bodyModel', {}).get('paragraphs', [])

                            if paragraphs:
                                logger.info(f"✅ Medium JSON API returned {len(paragraphs)} paragraphs")
                                # paragraph types: 1=p, 3=h1, 13=h2/h3, 6=blockquote, 8=ul/ol, 9=li
                                TYPE_MAP = {1: 'p', 3: 'h2', 13: 'h3', 6: 'blockquote', 4: 'h3'}
                                in_list = False
                                for para in paragraphs:
                                    ptype = para.get('type', 1)
                                    text = para.get('text', '').strip()
                                    if not text:
                                        continue
                                    if ptype in (8, 9):  # list item
                                        if not in_list:
                                            scraped_html += "<ul>\n"
                                            in_list = True
                                        scraped_html += f"<li>{text}</li>\n"
                                    else:
                                        if in_list:
                                            scraped_html += "</ul>\n"
                                            in_list = False
                                        tag = TYPE_MAP.get(ptype, 'p')
                                        scraped_html += f"<{tag}>{text}</{tag}>\n"
                                if in_list:
                                    scraped_html += "</ul>\n"
                        logger.info(f"📊 Medium JSON scrape: {len(scraped_html)} chars extracted")
                    except Exception as medium_err:
                        logger.warning(f"⚠️ Medium JSON API failed ({medium_err}), trying RSS feed...")

                    # Fallback: try Medium RSS feed for this publication
                    if not scraped_html:
                        try:
                            # Extract publication slug from URL
                            # e.g. medium.com/gooddollar/article-slug -> feed: medium.com/feed/gooddollar
                            url_parts = url.replace('https://', '').replace('http://', '').split('/')
                            # url_parts[0] = medium.com, [1] = pub or @user, [2] = article-slug
                            if len(url_parts) >= 3:
                                pub = url_parts[1]  # e.g. 'gooddollar' or '@username'
                                article_slug = url_parts[2].split('?')[0]
                                rss_url = f"https://medium.com/feed/{pub}"
                                logger.info(f"📡 Trying RSS feed: {rss_url}")
                                rss_resp = requests.get(rss_url, timeout=15, headers=medium_headers)
                                if rss_resp.status_code == 200:
                                    rss_soup = BeautifulSoup(rss_resp.content, 'xml')
                                    items = rss_soup.find_all('item')
                                    for item in items:
                                        item_link = item.find('link')
                                        link_text = item_link.get_text() if item_link else (item_link.next_sibling if item_link else '')
                                        if article_slug in str(link_text):
                                            content_encoded = item.find('content:encoded') or item.find('description')
                                            if content_encoded:
                                                article_soup = BeautifulSoup(content_encoded.get_text(), 'html.parser')
                                                for el in article_soup(['script', 'style', 'figure', 'img']):
                                                    el.decompose()
                                                for element in article_soup.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol']):
                                                    if element.name in ('h1', 'h2'):
                                                        scraped_html += f"<h2>{element.get_text().strip()}</h2>\n"
                                                    elif element.name == 'h3':
                                                        scraped_html += f"<h3>{element.get_text().strip()}</h3>\n"
                                                    elif element.name == 'p':
                                                        t = element.get_text().strip()
                                                        if t:
                                                            scraped_html += f"<p>{t}</p>\n"
                                                    elif element.name in ('ul', 'ol'):
                                                        tag = element.name
                                                        scraped_html += f"<{tag}>\n"
                                                        for li in element.find_all('li', recursive=False):
                                                            scraped_html += f"<li>{li.get_text().strip()}</li>\n"
                                                        scraped_html += f"</{tag}>\n"
                                                logger.info(f"✅ RSS feed scrape: {len(scraped_html)} chars")
                                            break
                        except Exception as rss_err:
                            logger.warning(f"⚠️ RSS fallback failed: {rss_err}")

                # --- Generic scrape for non-Medium URLs ---
                if not scraped_html:
                    logger.info(f"📥 Downloading webpage (generic scraper)...")
                    headers = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.9',
                        'Accept-Encoding': 'gzip, deflate, br',
                        'DNT': '1',
                        'Connection': 'keep-alive',
                        'Upgrade-Insecure-Requests': '1',
                        'Cache-Control': 'max-age=0',
                    }
                    response = requests.get(url, timeout=15, headers=headers, allow_redirects=True)
                    response.raise_for_status()
                    logger.info(f"✅ Webpage downloaded ({len(response.content)} bytes)")

                    soup = BeautifulSoup(response.content, 'html.parser')
                    for element in soup(['script', 'style', 'nav', 'footer', 'header', 'aside', 'iframe', 'noscript']):
                        element.decompose()

                    main_content = (
                        soup.find('article') or
                        soup.find('main') or
                        soup.find('div', class_='content') or
                        soup.find('div', class_='article') or
                        soup.find('body')
                    )

                    if main_content:
                        logger.info(f"📄 Found content container: {main_content.name}")
                        for element in main_content.find_all(['h1', 'h2', 'h3', 'p', 'ul', 'ol']):
                            if element.name == 'h1':
                                scraped_html += f"<h2>{element.get_text().strip()}</h2>\n"
                            elif element.name in ('h2', 'h3'):
                                scraped_html += f"<h3>{element.get_text().strip()}</h3>\n"
                            elif element.name == 'p':
                                text = element.get_text().strip()
                                if text:
                                    scraped_html += f"<p>{text}</p>\n"
                            elif element.name in ('ul', 'ol'):
                                tag = element.name
                                scraped_html += f"<{tag}>\n"
                                for li in element.find_all('li', recursive=False):
                                    scraped_html += f"<li>{li.get_text().strip()}</li>\n"
                                scraped_html += f"</{tag}>\n"

                if scraped_html.strip():
                    content = scraped_html.strip()
                    word_count = len(content.split())
                    reading_time_minutes = max(1, round(word_count / 200))
                    logger.info(f"✅ AUTO-SCRAPE SUCCESSFUL! {len(content)} chars, ~{reading_time_minutes} min read")
                else:
                    logger.warning(f"⚠️ Could not extract content from {url}")

            except Exception as scrape_error:
                logger.error(f"❌ Auto-scrape error: {scrape_error}")
                import traceback
                logger.error(f"🔍 Traceback: {traceback.format_exc()}")

                # Set warning but continue — do NOT block saving the link
                error_msg = str(scrape_error)
                if "403" in error_msg or "Forbidden" in error_msg:
                    scrape_warning = "Website blocked auto-scraping (403 Forbidden). The link was saved — please edit it to add content manually."
                elif "404" in error_msg:
                    scrape_warning = "Page not found (404). The link was saved — please check the URL and add content manually."
                elif "timeout" in error_msg.lower():
                    scrape_warning = "Request timed out. The link was saved — please edit it to add content manually."
                else:
                    scrape_warning = f"Could not auto-scrape content: {error_msg}. The link was saved — please edit it to add content manually."

        # Allow saving even without content (admin can edit later)
        if not content and not scrape_warning:
            return jsonify({"success": False, "error": "Content is required (or provide a URL for auto-scraping)"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        from datetime import datetime
        link_data = {
            'title': title,
            'url': url,
            'description': description,
            'content': content,
            'reading_time_minutes': reading_time_minutes,
            'display_order': display_order,
            'is_active': True,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat()
        }

        result = safe_supabase_operation(
            lambda: supabase.table('learn_earn_module_links').insert(link_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="add module link"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="add_module_link",
                action_details={"title": title, "url": url}
            )

            logger.info(f"✅ Module link added: {title}")
            response_data = {"success": True, "link": result.data[0]}
            if scrape_warning:
                response_data["warning"] = scrape_warning
            return jsonify(response_data)
        else:
            return jsonify({"success": False, "error": "Failed to add module link"}), 500

    except Exception as e:
        logger.error(f"❌ Add module link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/module-links/<int:link_id>", methods=["PUT"])
@admin_required
def update_module_link(link_id):
    """Update module link (admin only)"""
    try:
        data = request.json
        if not data:
            return jsonify({"success": False, "error": "No data provided"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        from datetime import datetime
        update_data = {}

        if 'title' in data:
            update_data['title'] = data['title'].strip()
        if 'url' in data:
            update_data['url'] = data['url'].strip()
        if 'description' in data:
            update_data['description'] = data['description'].strip()
        if 'content' in data:
            update_data['content'] = data['content'].strip()
        if 'reading_time_minutes' in data:
            update_data['reading_time_minutes'] = int(data['reading_time_minutes'])
        if 'display_order' in data:
            update_data['display_order'] = int(data['display_order'])
        if 'is_active' in data:
            update_data['is_active'] = data['is_active'] in [True, 'true', '1', 1]

        update_data['updated_at'] = datetime.utcnow().isoformat()

        result = safe_supabase_operation(
            lambda: supabase.table('learn_earn_module_links')\
                .update(update_data)\
                .eq('id', link_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="update module link"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_module_link",
                action_details={"link_id": link_id, "updated_fields": list(update_data.keys())}
            )

            logger.info(f"✅ Module link {link_id} updated")
            return jsonify({"success": True, "link": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Link not found"}), 404

    except Exception as e:
        logger.error(f"❌ Update module link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/module-links/<int:link_id>", methods=["DELETE"])
@admin_required
def delete_module_link(link_id):
    """Delete module link (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        result = safe_supabase_operation(
            lambda: supabase.table('learn_earn_module_links')\
                .delete()\
                .eq('id', link_id)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete module link"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="delete_module_link",
                action_details={"link_id": link_id}
            )

            logger.info(f"✅ Module link {link_id} deleted")
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Link not found"}), 404

    except Exception as e:
        logger.error(f"❌ Delete module link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/collaboration/submissions", methods=["GET"])
@admin_required
def get_collaboration_submissions():
    """List collaboration submissions for admin review."""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        status = (request.args.get('status') or '').strip().lower()
        query = supabase.table('collaboration_submissions').select('*').order('created_at', desc=True)
        if status and status != 'for_review':
            query = query.eq('status', status)
        result = safe_supabase_operation(
            lambda: query.execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get collaboration submissions"
        )
        submissions = result.data or []

        # Virtual filter for the admin review queue to hide already-finalized rows.
        if status == 'for_review':
            hidden_statuses = {'rejected', 'published'}
            submissions = [
                sub for sub in submissions
                if (sub.get('status') or '').strip().lower() not in hidden_statuses
            ]
        # Legacy-compat: older rows may use submission_id instead of id.
        for sub in submissions:
            if not sub.get('id') and sub.get('submission_id'):
                sub['id'] = sub.get('submission_id')
            sub = _try_backfill_collab_payment_metadata(supabase, sub)
            paid_amount, tx_hash = _collab_payment_metadata(sub)
            if not sub.get('paid_amount_gd') and paid_amount > 0:
                sub['paid_amount_gd'] = paid_amount
            if not sub.get('tx_hash') and tx_hash:
                sub['tx_hash'] = tx_hash
        return jsonify({
            "success": True,
            "submissions": submissions,
            "count": len(submissions)
        })
    except Exception as e:
        logger.error(f"❌ Get collaboration submissions error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _fetch_collaboration_submission(supabase, submission_identifier):
    """Fetch a collaboration submission by either id or legacy submission_id."""
    lookup_columns = ['id', 'submission_id']
    for column in lookup_columns:
        try:
            existing = safe_supabase_operation(
                lambda column=column: supabase.table('collaboration_submissions').select('*').eq(column, submission_identifier).limit(1).execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name=f"get collaboration submission by {column}"
            )
            if existing.data:
                return existing.data[0], column
        except Exception as lookup_error:
            logger.warning(f"⚠️ Collaboration lookup failed for column '{column}': {lookup_error}")
    return None, None


def _update_collaboration_submission(supabase, submission_identifier, payload, preferred_column):
    """Update a collaboration submission by preferred column, then fallback to legacy key."""
    columns = [preferred_column] if preferred_column else []
    for candidate in ('id', 'submission_id'):
        if candidate not in columns:
            columns.append(candidate)

    for column in columns:
        try:
            result = safe_supabase_operation(
                lambda column=column: supabase.table('collaboration_submissions').update(payload).eq(column, submission_identifier).select('*').execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name=f"update collaboration submission by {column}"
            )
            if result.data:
                return result

            # Some Supabase/PostgREST configurations can apply the update
            # but return an empty representation on UPDATE ... SELECT.
            # Retry with a minimal update call, then re-fetch to confirm.
            safe_supabase_operation(
                lambda column=column: supabase.table('collaboration_submissions').update(payload).eq(column, submission_identifier).execute(),
                fallback_result=None,
                operation_name=f"update collaboration submission by {column} (no select fallback)"
            )
            refreshed = safe_supabase_operation(
                lambda column=column: supabase.table('collaboration_submissions').select('*').eq(column, submission_identifier).limit(1).execute(),
                fallback_result=type('obj', (object,), {'data': []})(),
                operation_name=f"re-fetch collaboration submission by {column} after update"
            )
            refreshed_data = refreshed.data or []
            if refreshed_data:
                status_matches = (refreshed_data[0].get('status') or '').strip().lower() == (payload.get('status') or '').strip().lower()
                reason_matches = payload.get('rejection_reason') is None or refreshed_data[0].get('rejection_reason') == payload.get('rejection_reason')
                if status_matches and reason_matches:
                    return refreshed
        except Exception as update_error:
            logger.warning(f"⚠️ Collaboration update failed for column '{column}': {update_error}")

    return type('obj', (object,), {'data': []})()


def _collab_payment_metadata(submission: dict):
    """Read payment proof fields with backward compatibility across legacy column names."""
    tx_hash = (
        submission.get('tx_hash')
        or submission.get('transaction_hash')
        or ''
    )

    raw_amount = (
        submission.get('paid_amount_gd')
        if submission.get('paid_amount_gd') is not None
        else submission.get('amount_gd')
    )
    if raw_amount is None:
        raw_amount = submission.get('amount')

    try:
        paid_amount = float(raw_amount or 0)
    except (TypeError, ValueError):
        paid_amount = 0.0

    return paid_amount, str(tx_hash or '').strip()


def _try_backfill_collab_payment_metadata(supabase, submission: dict) -> dict:
    """Backfill missing collab payment proof from sponsorship_log for legacy rows."""
    if not isinstance(submission, dict):
        return submission

    paid_amount, tx_hash = _collab_payment_metadata(submission)
    if tx_hash and paid_amount > 0:
        return submission

    wallet_address = str(submission.get('wallet_address') or '').strip()
    if not wallet_address:
        return submission
    masked_wallet = (
        f"{wallet_address[:6]}...{wallet_address[-4:]}"
        if len(wallet_address) > 10 else wallet_address
    )

    submission_created_at = _parse_iso_datetime(submission.get('created_at'))
    exact_wallet_result = safe_supabase_operation(
        lambda: supabase.table('sponsorship_log')
            .select('tx_hash,amount_gd,created_at,wallet_address')
            .eq('wallet_address', wallet_address)
            .order('created_at', desc=True)
            .limit(10)
            .execute(),
        fallback_result=type('obj', (object,), {'data': []})(),
        operation_name="find fallback sponsorship log for collaboration submission (exact wallet)"
    )
    masked_wallet_result = safe_supabase_operation(
        lambda: supabase.table('sponsorship_log')
            .select('tx_hash,amount_gd,created_at,wallet_address')
            .eq('wallet_address', masked_wallet)
            .order('created_at', desc=True)
            .limit(10)
            .execute(),
        fallback_result=type('obj', (object,), {'data': []})(),
        operation_name="find fallback sponsorship log for collaboration submission (masked wallet)"
    )
    logs = (exact_wallet_result.data or []) + (masked_wallet_result.data or [])
    logs.sort(key=lambda row: str(row.get('created_at') or ''), reverse=True)
    if not logs:
        return submission

    matched_log = None
    for row in logs:
        row_tx = str(row.get('tx_hash') or '').strip()
        try:
            row_amt = float(row.get('amount_gd') or 0)
        except (TypeError, ValueError):
            row_amt = 0.0
        if not row_tx or row_amt <= 0:
            continue

        row_created_at = _parse_iso_datetime(row.get('created_at'))
        if submission_created_at and row_created_at and row_created_at < submission_created_at:
            continue
        matched_log = row
        break

    if not matched_log:
        return submission

    backfill_tx = str(matched_log.get('tx_hash') or '').strip()
    try:
        backfill_amount = float(matched_log.get('amount_gd') or 0)
    except (TypeError, ValueError):
        backfill_amount = 0.0

    if not backfill_tx or backfill_amount <= 0:
        return submission

    # Update response payload immediately so admin can review without refreshing DB state.
    submission['tx_hash'] = backfill_tx
    submission['paid_amount_gd'] = backfill_amount
    status = (submission.get('status') or '').strip().lower()
    if status in ('awaiting_payment', 'draft'):
        submission['status'] = 'paid'

    # Best-effort persistence so next reads are already normalized.
    persisted_payload = {
        'tx_hash': backfill_tx,
        'paid_amount_gd': backfill_amount,
        'updated_at': datetime.utcnow().isoformat() + 'Z'
    }
    if status in ('awaiting_payment', 'draft'):
        persisted_payload['status'] = 'paid'
    safe_supabase_operation(
        lambda: supabase.table('collaboration_submissions')
            .update(persisted_payload)
            .eq('id', submission.get('id'))
            .execute(),
        fallback_result=None,
        operation_name="persist fallback collaboration payment metadata"
    )
    return submission


@routes.route("/api/admin/collaboration/submissions/<submission_id>/approve", methods=["POST"])
@admin_required
def approve_collaboration_submission(submission_id):
    """Approve a paid collaboration submission for admin review workflow."""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        submission, matched_column = _fetch_collaboration_submission(supabase, submission_id)
        if not submission:
            return jsonify({"success": False, "error": "Submission not found"}), 404
        submission = _try_backfill_collab_payment_metadata(supabase, submission)

        current_status = (submission.get('status') or '').strip().lower()
        paid_amount, tx_hash = _collab_payment_metadata(submission)
        has_payment_proof = bool(tx_hash) or paid_amount > 0

        # Backward-compat: some rows may still be tagged awaiting_payment
        # even though payment metadata is already present.
        if current_status == 'awaiting_payment' and has_payment_proof:
            current_status = 'paid'

        if current_status == 'published':
            return jsonify({"success": False, "error": "Published submissions cannot be re-approved"}), 400
        if current_status not in ('paid', 'approved'):
            return jsonify({
                "success": False,
                "error": "Only submissions with detected payment proof can be approved."
            }), 400

        update_payload = {
            'status': 'approved',
            'rejection_reason': None,
            'updated_at': datetime.utcnow().isoformat() + 'Z'
        }
        result = _update_collaboration_submission(
            supabase=supabase,
            submission_identifier=submission_id,
            payload=update_payload,
            preferred_column=matched_column
        )
        if not (result.data or []):
            fallback_payload = {
                'status': 'approved',
                'updated_at': datetime.utcnow().isoformat() + 'Z'
            }
            logger.warning("⚠️ collaboration approve fallback: retrying without rejection_reason column.")
            result = _update_collaboration_submission(
                supabase=supabase,
                submission_identifier=submission_id,
                payload=fallback_payload,
                preferred_column=matched_column
            )
        if not (result.data or []):
            return jsonify({"success": False, "error": "Failed to update submission status to approved"}), 500

        automation_summary = {
            "modules_total": 0,
            "modules_enriched": 0,
            "draft_questions_created": 0
        }
        try:
            automation_summary = automate_collaboration_assets(
                supabase=supabase,
                submission_id=submission_id,
                question_count=15
            )
        except Exception as automation_error:
            logger.warning(f"⚠️ Collaboration automation on approve failed: {automation_error}")

        publish_result = _publish_collaboration_assets(
            supabase=supabase,
            submission_id=submission_id,
            replace_existing_questions=False
        )

        admin_wallet = session.get('wallet')
        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="approve_collaboration_submission",
            action_details={"submission_id": submission_id}
        )

        return jsonify({
            "success": True,
            "submission": (result.data or [submission])[0],
            "automation": automation_summary,
            "publish": publish_result
        })
    except Exception as e:
        logger.error(f"❌ Approve collaboration submission error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/collaboration/submissions/<submission_id>/reject", methods=["POST"])
@admin_required
def reject_collaboration_submission(submission_id):
    """Reject a collaboration submission and save an optional reason."""
    try:
        data = request.get_json(silent=True) or {}
        reason = (data.get('reason') or '').strip()

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        submission, matched_column = _fetch_collaboration_submission(supabase, submission_id)
        if not submission:
            return jsonify({"success": False, "error": "Submission not found"}), 404

        current_status = (submission.get('status') or '').strip().lower()
        if current_status == 'published':
            return jsonify({"success": False, "error": "Published submissions cannot be rejected"}), 400

        update_payload = {
            'status': 'rejected',
            'rejection_reason': reason or None,
            'updated_at': datetime.utcnow().isoformat() + 'Z'
        }
        result = _update_collaboration_submission(
            supabase=supabase,
            submission_identifier=submission_id,
            payload=update_payload,
            preferred_column=matched_column
        )
        if not (result.data or []):
            fallback_payload = {
                'status': 'rejected',
                'updated_at': datetime.utcnow().isoformat() + 'Z'
            }
            logger.warning("⚠️ collaboration reject fallback: rejection_reason column may be missing; retrying without reason field.")
            result = _update_collaboration_submission(
                supabase=supabase,
                submission_identifier=submission_id,
                payload=fallback_payload,
                preferred_column=matched_column
            )
        if not (result.data or []):
            return jsonify({"success": False, "error": "Failed to update submission status to rejected."}), 500

        admin_wallet = session.get('wallet')
        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="reject_collaboration_submission",
            action_details={"submission_id": submission_id, "reason": reason}
        )

        return jsonify({
            "success": True,
            "submission": (result.data or [submission])[0]
        })
    except Exception as e:
        logger.error(f"❌ Reject collaboration submission error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/collaboration/submissions/<submission_id>/generate-quiz-draft", methods=["POST"])
@admin_required
def generate_collaboration_quiz_draft(submission_id):
    """Generate draft quiz questions from collaboration modules in current quiz format."""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        sub = safe_supabase_operation(
            lambda: supabase.table('collaboration_submissions').select('*').eq('id', submission_id).limit(1).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get collaboration submission"
        )
        if not sub.data:
            return jsonify({"success": False, "error": "Submission not found"}), 404

        created = generate_collaboration_quiz_draft_rows(
            supabase=supabase,
            submission_id=submission_id,
            question_count=15
        )
        if created <= 0:
            return jsonify({"success": False, "error": "No active collaboration modules found"}), 400

        return jsonify({"success": True, "created_count": created})
    except Exception as e:
        logger.error(f"❌ Generate collaboration draft questions error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


def _publish_collaboration_assets(supabase, submission_id, replace_existing_questions=False):
    """Publish collaboration module rows and generated draft quiz questions into live Learn & Earn tables."""
    submission_res = safe_supabase_operation(
        lambda: supabase.table('collaboration_submissions').select('*').eq('id', submission_id).limit(1).execute(),
        fallback_result=type('obj', (object,), {'data': []})(),
        operation_name="get collaboration submission for publish helper"
    )
    if not submission_res.data:
        raise ValueError("Submission not found")

    submission = submission_res.data[0]
    if submission.get('status') not in ('paid', 'approved', 'published'):
        raise ValueError("Only paid or approved submissions can be published")

    modules = safe_supabase_operation(
        lambda: supabase.table('collaboration_modules').select('*')
            .eq('submission_id', submission_id)
            .eq('is_deleted', False)
            .eq('is_active', True)
            .order('display_order', desc=False)
            .execute(),
        fallback_result=type('obj', (object,), {'data': []})(),
        operation_name="get modules for publish helper"
    )
    if not modules.data:
        raise ValueError("No active modules to publish")

    inserted_modules = 0
    for module in modules.data:
        link_row = {
            'title': module.get('title'),
            'url': module.get('url') or '',
            'description': f"Collaboration module from {submission.get('partner_name', 'partner')}",
            'content': module.get('content') or '',
            'reading_time_minutes': module.get('reading_time_minutes') or 1,
            'display_order': module.get('display_order') or 1,
            'is_active': True,
            'created_at': datetime.utcnow().isoformat(),
            'updated_at': datetime.utcnow().isoformat()
        }
        safe_supabase_operation(
            lambda row=link_row: supabase.table('learn_earn_module_links').insert(row).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="publish collaboration module helper"
        )
        inserted_modules += 1

    draft_q = safe_supabase_operation(
        lambda: supabase.table('collaboration_quiz_questions_draft').select('*').eq('submission_id', submission_id).execute(),
        fallback_result=type('obj', (object,), {'data': []})(),
        operation_name="fetch collaboration draft questions helper"
    )

    if replace_existing_questions:
        safe_supabase_operation(
            lambda: supabase.table('quiz_questions').delete().neq('quiz_id', 0).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="replace existing quiz questions helper"
        )

    inserted_questions = 0
    for q in draft_q.data or []:
        question_data = {
            'question_id': q.get('question_id'),
            'question': q.get('question'),
            'answer_a': q.get('answer_a'),
            'answer_b': q.get('answer_b'),
            'answer_c': q.get('answer_c'),
            'answer_d': q.get('answer_d'),
            'correct': q.get('correct'),
            'created_at': datetime.utcnow().isoformat() + 'Z'
        }
        safe_supabase_operation(
            lambda row=question_data: supabase.table('quiz_questions').insert(row).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="publish collaboration quiz question helper"
        )
        inserted_questions += 1

    safe_supabase_operation(
        lambda: supabase.table('collaboration_submissions')
            .update({'status': 'published', 'updated_at': datetime.utcnow().isoformat() + 'Z'})
            .eq('id', submission_id)
            .execute(),
        fallback_result=type('obj', (object,), {'data': []})(),
        operation_name="mark collaboration published helper"
    )

    return {
        "published_modules": inserted_modules,
        "published_questions": inserted_questions,
        "replaced_existing_questions": replace_existing_questions
    }


@routes.route("/api/admin/collaboration/submissions/<submission_id>/publish", methods=["POST"])
@admin_required
def publish_collaboration_submission(submission_id):
    """Publish collaboration modules/questions into live Learn & Earn content."""
    try:
        data = request.get_json(silent=True) or {}
        replace_existing_questions = bool(data.get('replace_existing_questions', False))

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Ensure latest module content + fixed 15-question draft exists before publish.
        automate_collaboration_assets(
            supabase=supabase,
            submission_id=submission_id,
            question_count=15
        )

        publish_result = _publish_collaboration_assets(
            supabase=supabase,
            submission_id=submission_id,
            replace_existing_questions=replace_existing_questions
        )

        return jsonify({
            "success": True,
            **publish_result
        })
    except Exception as e:
        logger.error(f"❌ Publish collaboration submission error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/admin")
@auth_required
def admin_dashboard():
    """Admin dashboard page"""
    wallet = session.get("wallet")

    from supabase_client import is_admin
    if not is_admin(wallet):
        logger.warning(f"⚠️ Non-admin access attempt from {wallet[:8]}...")
        return redirect("/dashboard")

    logger.info(f"✅ Admin access granted to {wallet[:8]}...")

    response = make_response(render_template("admin_dashboard.html", wallet=wallet))
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response

@routes.route("/learn-earn")
def learn_earn_page():
    if not session.get("verified") or not session.get("wallet"):
        return redirect(url_for("routes.index"))

    wallet = session.get("wallet")
    is_admin_user = False
    try:
        from supabase_client import is_admin
        is_admin_user = bool(is_admin(wallet))
    except Exception as admin_err:
        logger.warning(f"⚠️ Could not resolve admin status for /learn-earn route: {admin_err}")

    # Track Learn & Earn page visit
    analytics.track_page_view(wallet, "learn_earn")

    return render_template("learn_and_earn.html",
                         wallet=wallet,
                         login_method=session.get("login_method", "walletconnect"),
                         walletconnect_project_id=os.environ.get("WALLETCONNECT_PROJECT_ID", ""),
                         walletconnect_sidecar_enabled=_is_walletconnect_sidecar_enabled(),
                         is_admin_user=is_admin_user)

@routes.route('/api/p2p/history')
def get_p2p_history_api():
    """P2P trading has been removed - return empty history"""
    try:
        wallet = session.get('wallet')
        if not wallet or not session.get('verified'):
            return jsonify({"success": False, "error": "Not authenticated"}), 401

        logger.info(f"📋 P2P trading disabled - returning empty history for {wallet[:8]}...")

        return jsonify({
            "success": True,
            "trades": [],
            "total": 0,
            "message": "P2P trading feature has been disabled"
        })

    except Exception as e:
        logger.error(f"❌ Error in P2P history endpoint: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "trades": [],
            "total": 0
        }), 500

@routes.route("/api/admin/community-stories-notifications", methods=["GET"])
@admin_required
def get_admin_notifications():
    """Get pending submissions for admin"""
    try:
        wallet = session.get("wallet")

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        # Get pending submissions directly to include storage_path
        pending = safe_supabase_operation(
            lambda: supabase.table('community_stories_submissions')\
                .select('*')\
                .eq('status', 'pending')\
                .order('submitted_at', desc=True)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get pending community stories"
        )

        # Format for admin display
        notifications = []
        if pending.data:
            for sub in pending.data:
                notifications.append({
                    'submission_id': sub.get('submission_id'),
                    'community_stories_submissions': sub
                })

        return jsonify({
            "success": True,
            "notifications": notifications,
            "count": len(notifications)
        })

    except Exception as e:
        logger.error(f"❌ Error getting admin notifications: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/admin/developer-profile", methods=["POST"])
@admin_required
def upload_developer_profile():
    """Upload developer profile image (admin only) - supports multiple profiles"""
    try:
        from object_storage_client import upload_to_imgbb

        if 'image' not in request.files:
            return jsonify({"success": False, "error": "No image file provided"}), 400

        image_file = request.files['image']
        name = request.form.get('name', '').strip()
        position = request.form.get('position', '').strip()

        if not name or not position:
            return jsonify({"success": False, "error": "Name and position are required"}), 400

        # Upload to ImgBB
        upload_result = upload_to_imgbb(image_file)

        if not upload_result.get('success'):
            return jsonify({"success": False, "error": upload_result.get('error', 'Upload failed')}), 500

        image_url = upload_result.get('url')

        # Store in database
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        from datetime import datetime
        profile_data = {
            'name': name,
            'position': position,
            'image_url': image_url,
            'is_active': True,
            'created_at': datetime.utcnow().isoformat()
        }

        # Always insert new profile (allows multiple developers)
        result = safe_supabase_operation(
            lambda: supabase.table('developer_profile').insert(profile_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="insert developer profile"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="upload_developer_profile",
                action_details={"name": name, "position": position}
            )

            logger.info(f"✅ Developer profile uploaded: {name}")
            return jsonify({"success": True, "profile": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Failed to save profile"}), 500

    except Exception as e:
        logger.error(f"❌ Upload developer profile error: {e}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        return jsonify({"success": False, "error": str(e)}), 500

@routes.route("/api/developer-profile", methods=["GET"])
def get_developer_profile():
    """Get all active developer profiles for homepage"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "profiles": []})

        result = safe_supabase_operation(
            lambda: supabase.table('developer_profile')\
                .select('*')\
                .eq('is_active', True)\
                .order('created_at', desc=False)\
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get developer profiles"
        )

        profiles = result.data if result.data else []

        return jsonify({
            "success": True,
            "profiles": profiles,
            "count": len(profiles)
        })

    except Exception as e:
        logger.error(f"❌ Get developer profiles error: {e}")
        return jsonify({"success": False, "profiles": []})


# ============================================================
# DISCOURSE TASK ROUTES
# ============================================================

@routes.route("/api/discourse-task/settings", methods=["GET"])
def get_discourse_task_settings():
    """Get discourse task settings and current user status"""
    try:
        wallet = session.get('wallet')
        if not wallet:
            return jsonify({"success": False, "error": "Not authenticated"}), 401
        from discourse_task.discourse_task import discourse_task_service
        settings = discourse_task_service.get_settings()
        current_link = settings.get('link')
        user_status = discourse_task_service.get_user_status(wallet, current_link)
        return jsonify({
            "success": True,
            "link": current_link,
            "reward_amount": settings.get('reward_amount'),
            "user_status": user_status.get('status'),
            "discourse_username": user_status.get('discourse_username'),
            "submitted_at": user_status.get('submitted_at'),
            "tx_hash": user_status.get('tx_hash')
        })
    except Exception as e:
        logger.error(f"❌ Error getting discourse task settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/discourse-task/submit", methods=["POST"])
def submit_discourse_username():
    """Submit discourse username for approval"""
    try:
        wallet = session.get('wallet')
        if not wallet:
            return jsonify({"success": False, "error": "Not authenticated"}), 401
        from discourse_task.discourse_task import discourse_task_service
        data = request.json
        discourse_username = data.get('discourse_username', '').strip()
        discourse_link = data.get('discourse_link', '').strip()

        if not discourse_username:
            return jsonify({"success": False, "error": "Discourse username is required"}), 400

        result = discourse_task_service.submit_username(wallet, discourse_username, discourse_link or None)
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error submitting discourse username: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/settings", methods=["GET"])
@admin_required
def admin_get_discourse_settings():
    """Admin: Get discourse task settings"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        settings = discourse_task_service.get_settings()
        return jsonify(settings)
    except Exception as e:
        logger.error(f"❌ Error getting discourse admin settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/settings", methods=["POST"])
@admin_required
def admin_update_discourse_settings():
    """Admin: Update discourse task settings"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        data = request.json
        discourse_link = data.get('discourse_link', '').strip()
        reward_amount = float(data.get('reward_amount', 500))
        admin_wallet = session.get('wallet')

        result = discourse_task_service.update_settings(discourse_link, reward_amount, admin_wallet)

        if result.get('success'):
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="update_discourse_task_settings",
                action_details={"link": discourse_link, "reward_amount": reward_amount}
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error updating discourse settings: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/pending", methods=["GET"])
@admin_required
def admin_get_discourse_pending():
    """Admin: Get pending discourse task submissions"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        result = discourse_task_service.get_pending_submissions()
        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error getting pending discourse submissions: {e}")
        return jsonify({"success": False, "submissions": [], "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/approve", methods=["POST"])
@admin_required
def admin_approve_discourse():
    """Admin: Approve a discourse task submission and disburse reward"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        data = request.json
        submission_id = data.get('submission_id')
        admin_wallet = session.get('wallet')

        if not submission_id:
            return jsonify({"success": False, "error": "Missing submission_id"}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                discourse_task_service.approve_submission(submission_id, admin_wallet)
            )
        finally:
            loop.close()

        if result and result.get('success'):
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="approve_discourse_task",
                action_details={"submission_id": submission_id}
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error approving discourse submission: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/discourse-task/reject", methods=["POST"])
@admin_required
def admin_reject_discourse():
    """Admin: Reject a discourse task submission"""
    try:
        from discourse_task.discourse_task import discourse_task_service
        data = request.json
        submission_id = data.get('submission_id')
        reason = data.get('reason', '')
        admin_wallet = session.get('wallet')

        if not submission_id:
            return jsonify({"success": False, "error": "Missing submission_id"}), 400

        import asyncio
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                discourse_task_service.reject_submission(submission_id, admin_wallet, reason)
            )
        finally:
            loop.close()

        if result and result.get('success'):
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="reject_discourse_task",
                action_details={"submission_id": submission_id, "reason": reason}
            )

        return jsonify(result)
    except Exception as e:
        logger.error(f"❌ Error rejecting discourse submission: {e}")
        return jsonify({"success": False, "error": str(e)}), 500

# ─────────────────────────────────────────────
# YouTube Video Management (Homepage Carousel)
# ─────────────────────────────────────────────

@routes.route("/api/youtube-videos", methods=["GET"])
def get_youtube_videos_public():
    """Get all active YouTube videos for homepage carousel"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "videos": []})

        result = safe_supabase_operation(
            lambda: supabase.table('homepage_videos')
                .select('*')
                .eq('is_active', True)
                .order('created_at', desc=True)
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="get homepage videos"
        )

        return jsonify({
            "success": True,
            "videos": result.data if result.data else []
        })

    except Exception as e:
        logger.error(f"❌ Error getting homepage videos: {e}")
        return jsonify({"success": True, "videos": []})


@routes.route("/api/sponsor-certificates", methods=["GET"])
def get_sponsor_certificates():
    """Get sponsor certificates for homepage carousel"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "certificates": []})

        try:
            result = supabase.table('sponsor_certificates')\
                .select('*')\
                .eq('is_active', True)\
                .order('created_at', desc=True)\
                .execute()
            return jsonify({
                "success": True,
                "certificates": result.data if result.data else []
            })
        except Exception:
            return jsonify({"success": True, "certificates": []})

    except Exception as e:
        logger.error(f"❌ Error getting sponsor certificates: {e}")
        return jsonify({"success": True, "certificates": []})


@routes.route("/api/admin/youtube-videos", methods=["GET"])
@admin_required
def admin_get_youtube_videos():
    """Get all YouTube videos (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        result = safe_supabase_operation(
            lambda: supabase.table('homepage_videos')
                .select('*')
                .order('created_at', desc=True)
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="admin get homepage videos"
        )

        return jsonify({
            "success": True,
            "videos": result.data if result.data else []
        })

    except Exception as e:
        logger.error(f"❌ Error getting homepage videos (admin): {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/youtube-videos", methods=["POST"])
@admin_required
def admin_add_youtube_video():
    """Add a YouTube video link (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        data = request.get_json()
        youtube_url = (data.get('youtube_url') or '').strip()
        title = (data.get('title') or '').strip()

        if not youtube_url:
            return jsonify({"success": False, "error": "YouTube URL is required"}), 400

        # Extract YouTube video ID from various URL formats
        import re
        yt_id_match = re.search(
            r'(?:youtube\.com\/(?:watch\?v=|embed\/|shorts\/)|youtu\.be\/)([A-Za-z0-9_-]{11})',
            youtube_url
        )
        if not yt_id_match:
            return jsonify({"success": False, "error": "Invalid YouTube URL. Please use a standard YouTube link."}), 400

        video_id = yt_id_match.group(1)
        embed_url = f"https://www.youtube.com/embed/{video_id}"

        video_data = {
            "youtube_url": youtube_url,
            "embed_url": embed_url,
            "video_id": video_id,
            "title": title or "GoodMarket Video",
            "is_active": True
        }

        result = safe_supabase_operation(
            lambda: supabase.table('homepage_videos').insert(video_data).execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="add homepage video"
        )

        if result.data:
            admin_wallet = session.get('wallet')
            log_admin_action(
                admin_wallet=admin_wallet,
                action_type="add_youtube_video",
                action_details={"video_id": video_id, "title": title}
            )
            logger.info(f"✅ YouTube video added by admin {admin_wallet[:8] if admin_wallet else 'unknown'}...")
            return jsonify({"success": True, "video": result.data[0]})
        else:
            return jsonify({"success": False, "error": "Failed to save video. Make sure the homepage_videos table exists in Supabase."}), 500

    except Exception as e:
        logger.error(f"❌ Error adding YouTube video: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/profile", methods=["GET"])
@auth_required
def get_profile():
    """Get user profile data including earnings breakdown and activity history"""
    try:
        wallet = session.get("wallet")
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        user_info = safe_supabase_operation(
            lambda: supabase.table("user_data")
                .select("wallet_address, first_login, last_login, ubi_verified")
                .eq("wallet_address", wallet)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get user info"
        )

        masked_wallet = f"{wallet[:6]}...{wallet[-4:]}"
        wallet_lower = wallet.lower()
        learn_data = safe_supabase_operation(
            lambda: supabase.table("learnearn_log")
                .select("amount_g$, timestamp, score, total_questions, quiz_id")
                .or_(f"wallet_address.eq.{masked_wallet},wallet_address.eq.{wallet_lower},wallet_address.eq.{wallet}")
                .eq("status", True)
                .order("timestamp", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get learn data"
        )

        twitter_data = safe_supabase_operation(
            lambda: supabase.table("twitter_task_log")
                .select("reward_amount, status, created_at, twitter_url")
                .eq("wallet_address", wallet)
                .eq("status", "completed")
                .order("created_at", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get twitter data"
        )

        telegram_data = safe_supabase_operation(
            lambda: supabase.table("telegram_task_log")
                .select("reward_amount, status, created_at, telegram_url")
                .eq("wallet_address", wallet)
                .eq("status", "completed")
                .order("created_at", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get telegram data"
        )

        stories_data = safe_supabase_operation(
            lambda: supabase.table("community_stories_submissions")
                .select("reward_amount, status, created_at")
                .eq("wallet_address", wallet)
                .eq("status", "approved")
                .order("created_at", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get stories data"
        )

        price_pred_data = safe_supabase_operation(
            lambda: supabase.table("price_predictions")
                .select("crypto_symbol, direction, timeframe_minutes, entry_price, result_price, resolved_at")
                .eq("wallet_address", wallet)
                .eq("status", "won")
                .order("resolved_at", desc=True)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get price prediction data"
        )

        learn_records_raw = learn_data.data or []
        seen_quiz_ids = set()
        learn_records = []
        for r in learn_records_raw:
            qid = r.get("quiz_id")
            if qid and qid not in seen_quiz_ids:
                seen_quiz_ids.add(qid)
                learn_records.append(r)
            elif not qid:
                learn_records.append(r)

        twitter_records = twitter_data.data or []
        telegram_records = telegram_data.data or []
        stories_records = stories_data.data or []
        price_pred_records = price_pred_data.data or []
        user_records = user_info.data or []

        _pp_rewards = {1: 2.0, 60: 5.0, 720: 20.0, 1440: 50.0}

        learn_total = sum(float(r.get("amount_g$") or 0) for r in learn_records)
        twitter_total = sum(float(r.get("reward_amount") or 0) for r in twitter_records)
        telegram_total = sum(float(r.get("reward_amount") or 0) for r in telegram_records)
        stories_total = sum(float(r.get("reward_amount") or 0) for r in stories_records)
        price_pred_total = sum(_pp_rewards.get(int(r.get("timeframe_minutes") or 0), 0) for r in price_pred_records)
        grand_total = learn_total + twitter_total + telegram_total + stories_total + price_pred_total

        user_row = user_records[0] if user_records else {}
        first_login = user_row.get("first_login") or user_row.get("last_login")

        recent_activity = []
        for r in learn_records[:5]:
            recent_activity.append({
                "type": "Learn & Earn",
                "icon": "🎓",
                "amount": float(r.get("amount_g$") or 0),
                "date": r.get("timestamp")
            })
        for r in twitter_records[:3]:
            recent_activity.append({
                "type": "Twitter Task",
                "icon": "🐦",
                "amount": float(r.get("reward_amount") or 0),
                "date": r.get("created_at")
            })
        for r in telegram_records[:3]:
            recent_activity.append({
                "type": "Telegram Task",
                "icon": "📱",
                "amount": float(r.get("reward_amount") or 0),
                "date": r.get("created_at")
            })
        for r in stories_records[:3]:
            recent_activity.append({
                "type": "Community Story",
                "icon": "🌟",
                "amount": float(r.get("reward_amount") or 0),
                "date": r.get("created_at")
            })
        for r in price_pred_records[:5]:
            mins = int(r.get("timeframe_minutes") or 0)
            reward = _pp_rewards.get(mins, 0)
            crypto = r.get("crypto_symbol", "")
            direction = r.get("direction", "")
            tf_labels = {1: "1 Min", 60: "1 Hour", 720: "12 Hours", 1440: "24 Hours"}
            tf_label = tf_labels.get(mins, f"{mins}min")
            detail = f"{crypto} {direction} ({tf_label})"
            recent_activity.append({
                "type": f"Price Prediction Win — {detail}",
                "icon": "📈",
                "amount": reward,
                "date": r.get("resolved_at")
            })

        recent_activity.sort(key=lambda x: x.get("date") or "", reverse=True)

        return jsonify({
            "success": True,
            "wallet": wallet,
            "first_login": first_login,
            "earnings": {
                "learn_earn": round(learn_total, 2),
                "twitter": round(twitter_total, 2),
                "telegram": round(telegram_total, 2),
                "community_stories": round(stories_total, 2),
                "price_prediction": round(price_pred_total, 2),
                "total": round(grand_total, 2)
            },
            "counts": {
                "quizzes": len(learn_records),
                "twitter_tasks": len(twitter_records),
                "telegram_tasks": len(telegram_records),
                "stories": len(stories_records),
                "price_predictions": len(price_pred_records)
            },
            "recent_activity": recent_activity[:15]
        })

    except Exception as e:
        logger.error(f"❌ Error fetching profile: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/youtube-videos/<int:video_id>", methods=["DELETE"])
@admin_required
def admin_delete_youtube_video(video_id):
    """Delete a YouTube video (admin only)"""
    try:
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database not available"}), 500

        result = safe_supabase_operation(
            lambda: supabase.table('homepage_videos')
                .delete()
                .eq('id', video_id)
                .execute(),
            fallback_result=type('obj', (object,), {'data': []})(),
            operation_name="delete homepage video"
        )

        admin_wallet = session.get('wallet')
        log_admin_action(
            admin_wallet=admin_wallet,
            action_type="delete_youtube_video",
            action_details={"video_id": video_id}
        )

        logger.info(f"✅ YouTube video {video_id} deleted by admin {admin_wallet[:8] if admin_wallet else 'unknown'}...")
        return jsonify({"success": True})

    except Exception as e:
        logger.error(f"❌ Error deleting YouTube video: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/check-identity", methods=["GET"])
def check_identity():
    """Check if a wallet address is face-verified on the GoodDollar Identity contract."""
    wallet_address = request.args.get("wallet", "").strip()
    if not wallet_address:
        return jsonify({"error": "wallet param required"}), 400
    try:
        from web3 import Web3
        wallet_address = Web3.to_checksum_address(wallet_address)
    except Exception:
        return jsonify({"error": "Invalid wallet address"}), 400

    from blockchain import is_identity_verified
    result = is_identity_verified(wallet_address)
    return jsonify(result)


def _wc_service_url():
    base = os.getenv("WC_SERVICE_URL")
    if base:
        return base.rstrip("/")
    return f"http://127.0.0.1:{os.getenv('WC_SERVICE_PORT', '3001')}"




def _is_walletconnect_sidecar_enabled() -> bool:
    has_explicit_sidecar = bool(os.getenv("WC_SERVICE_URL"))
    is_serverless_runtime = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
    return has_explicit_sidecar or not is_serverless_runtime

def _wc_proxy(method: str, path: str, body: dict = None, timeout: int = 30):
    if not _is_walletconnect_sidecar_enabled():
        return None, 503, "WalletConnect sidecar unavailable in serverless runtime"

    url = f"{_wc_service_url()}{path}"
    payload = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method=method.upper(),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            data = json.loads(raw) if raw else {}
            return data, resp.status, None
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8")
            err_json = json.loads(err_body) if err_body else {}
            return err_json, e.code, None
        except Exception:
            return {"error": f"HTTP {e.code}"}, e.code, None
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return None, 503, f"WalletConnect service unavailable: {reason}"
    except Exception as e:
        return None, 500, str(e)


@routes.route("/api/wc-uri", methods=["GET"])
def wc_uri():
    data, status, err = _wc_proxy("GET", "/uri", timeout=35)
    if err:
        return jsonify({"success": False, "error": err}), status
    if status >= 400:
        return jsonify({"success": False, **(data or {})}), status
    return jsonify({"success": True, **(data or {})}), 200


@routes.route("/api/wc-session/<session_id>", methods=["GET"])
def wc_session(session_id):
    safe_id = str(session_id).strip()
    if not safe_id:
        return jsonify({"success": False, "error": "session_id required"}), 400
    data, status, err = _wc_proxy("GET", f"/session/{safe_id}", timeout=20)
    if err:
        return jsonify({"success": False, "error": err}), status
    if status >= 400:
        return jsonify({"success": False, **(data or {})}), status
    return jsonify({"success": True, **(data or {})}), 200


@routes.route("/api/wc-sign/<session_id>", methods=["POST"])
def wc_sign(session_id):
    safe_id = str(session_id).strip()
    if not safe_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    body = request.get_json(silent=True) or {}
    message = (body.get("message") or "").strip()
    address = (body.get("address") or "").strip()
    if not message or not address:
        return jsonify({"success": False, "error": "message and address are required"}), 400

    data, status, err = _wc_proxy(
        "POST",
        f"/sign/{safe_id}",
        body={"message": message, "address": address},
        timeout=45,
    )
    if err:
        return jsonify({"success": False, "error": err}), status
    if status >= 400:
        return jsonify({"success": False, **(data or {})}), status
    return jsonify({"success": True, **(data or {})}), 200


@routes.route("/api/wc-tx/<session_id>", methods=["POST"])
def wc_tx(session_id):
    safe_id = str(session_id).strip()
    if not safe_id:
        return jsonify({"success": False, "error": "session_id required"}), 400

    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"success": False, "error": "invalid request body"}), 400

    data, status, err = _wc_proxy(
        "POST",
        f"/tx/{safe_id}",
        body=body,
        timeout=60,
    )
    if err:
        return jsonify({"success": False, "error": err}), status
    if status >= 400:
        return jsonify({"success": False, **(data or {})}), status
    return jsonify({"success": True, **(data or {})}), 200




@routes.route("/api/tx-receipt/<tx_hash>", methods=["GET"])
@auth_required
def tx_receipt(tx_hash):
    """Poll Celo for a transaction receipt and return its status."""
    try:
        from web3 import Web3
        import blockchain as _bc
        w3 = Web3(Web3.HTTPProvider(_bc.CELO_RPC, request_kwargs={"timeout": 10}))
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            return jsonify({"found": False, "status": "pending"})
        return jsonify({
            "found": True,
            "status": "success" if receipt.status == 1 else "failed",
            "block_number": receipt.blockNumber,
            "gas_used": receipt.gasUsed
        })
    except Exception as e:
        logger.error(f"tx_receipt error for {tx_hash}: {e}")
        return jsonify({"found": False, "status": "pending", "error": str(e)})


@routes.route("/api/xdc/tx-revert-reason/<tx_hash>", methods=["GET"])
@auth_required
def xdc_tx_revert_reason(tx_hash):
    """Fetch exact-ish revert reason for a mined failed tx on XDC, if available."""
    try:
        from web3 import Web3
        from blockchain import XDC_RPC

        w3 = Web3(Web3.HTTPProvider(XDC_RPC, request_kwargs={"timeout": 12}))
        receipt = w3.eth.get_transaction_receipt(tx_hash)
        if receipt is None:
            return jsonify({"success": True, "found": False, "status": "pending"})
        if int(receipt.get("status", 0)) == 1:
            return jsonify({"success": True, "found": True, "status": "success", "reverted": False, "reason": None})

        tx = w3.eth.get_transaction(tx_hash)
        bridge_context = _decode_bridge_to_input(tx.get("input", "0x"))
        call_obj = {
            "from": tx.get("from"),
            "to": tx.get("to"),
            "data": tx.get("input", "0x"),
            "value": tx.get("value", 0),
        }
        replay_block = max(int(receipt.get("blockNumber", 0)) - 1, 0)

        reason_data = _extract_xdc_revert_reason(
            w3,
            call_obj,
            replay_block,
            fallback_reason="Transaction reverted on XDC."
        )
        logger.error(
            "xdc_tx_revert_reason: tx=%s selector=%s category=%s from=%s to=%s value_wei=%s bridge_ctx=%s reason=%s",
            tx_hash,
            reason_data.get("error_selector"),
            reason_data.get("error_category"),
            tx.get("from"),
            tx.get("to"),
            tx.get("value"),
            bridge_context,
            reason_data.get("reason"),
        )

        return jsonify({
            "success": True,
            "found": True,
            "status": "failed",
            "reverted": True,
            "reason": reason_data.get("reason"),
            "technical_details": reason_data.get("technical_details"),
            "error_selector": reason_data.get("error_selector"),
            "error_category": reason_data.get("error_category"),
            "bridge_context": bridge_context,
            "tx_hash": tx_hash,
            "block_number": receipt.get("blockNumber"),
        })
    except Exception as e:
        logger.error(f"xdc_tx_revert_reason error for {tx_hash}: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/ubi-entitlement", methods=["GET"])
@auth_required
def ubi_entitlement():
    """Return how much G$ the logged-in wallet can claim right now."""
    try:
        wallet = session.get("wallet")
        force  = request.args.get("force", "0") == "1"
        from blockchain import get_ubi_entitlement, invalidate_entitlement_cache
        if force:
            invalidate_entitlement_cache(wallet)
        result = get_ubi_entitlement(wallet)
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        logger.error(f"UBI entitlement route error: {e}")
        return jsonify({"success": False, "error": str(e), "entitlement": 0, "can_claim": False}), 500


@routes.route("/api/claim/availability", methods=["GET"])
@auth_required
def claim_availability():
    """Return per-network GoodDollar claim availability for the logged-in wallet."""
    try:
        wallet = session.get("wallet")
        force = request.args.get("force", "0") == "1"
        from blockchain import (
            get_ubi_entitlement,
            invalidate_entitlement_cache,
            check_xdc_ubi_entitlement,
        )
        if force:
            invalidate_entitlement_cache(wallet)

        celo = get_ubi_entitlement(wallet)
        # Fuse claiming is temporarily disabled because the Fuse claim contract
        # balance is too low. Keep the network visible as "Not Available" but
        # never recommend it or let the frontend treat it as claimable.
        fuse = {
            "success": True,
            "can_claim": False,
            "claimable": 0,
            "chain_id": int(os.getenv("FUSE_CHAIN_ID", "122")),
            "error": "Fuse claim is temporarily not available.",
            "is_available": False,
        }
        xdc = check_xdc_ubi_entitlement(wallet)

        claims = {
            "celo": {
                "network": "celo",
                "label": "Celo",
                "success": bool(celo.get("success")),
                "can_claim": bool(celo.get("can_claim")),
                "claimable": float(celo.get("entitlement") or 0),
                "claimable_formatted": celo.get("entitlement_formatted") or f"{float(celo.get('entitlement') or 0):.2f}",
                "reason": celo.get("reason"),
                "is_verified": celo.get("is_verified"),
                "ubi_contract": celo.get("ubi_contract"),
                "chain_id": 42220,
                "error": celo.get("error"),
            },
            "fuse": {
                "network": "fuse",
                "label": "Fuse",
                "success": bool(fuse.get("success")),
                "can_claim": False,
                "is_available": bool(fuse.get("is_available", True)),
                "claimable": float(fuse.get("claimable") or 0),
                "claimable_formatted": f"{float(fuse.get('claimable') or 0):.2f}",
                "ubi_contract": fuse.get("ubi_contract"),
                "chain_id": fuse.get("chain_id", 122),
                "error": fuse.get("error"),
            },
            "xdc": {
                "network": "xdc",
                "label": "XDC",
                "success": bool(xdc.get("success")),
                "can_claim": bool(xdc.get("can_claim")),
                "claimable": float(xdc.get("claimable") or 0),
                "claimable_formatted": f"{float(xdc.get('claimable') or 0):.2f}",
                "chain_id": 50,
                "error": xdc.get("error"),
            },
        }

        celo_reason = claims["celo"].get("reason")
        celo_verified = claims["celo"].get("is_verified")
        celo_needs_face_verification = (
            celo_reason in ("not_verified", "re_verification_needed")
            or celo_verified is False
        )

        # Face Verification is anchored on Celo Identity. If Celo says the user
        # still needs (re)verification, do not allow fallback claim actions on
        # other networks yet.
        if celo_needs_face_verification:
            for network in ("fuse", "xdc"):
                claims[network]["can_claim"] = False
                claims[network]["blocked_by"] = "celo_identity_verification"
                claims[network]["blocked_reason"] = "Verify Face ID on Celo first."

        recommended = None
        for network in ("celo", "fuse", "xdc"):
            if claims[network]["can_claim"]:
                recommended = network
                break

        response = jsonify({
            "success": True,
            "wallet": wallet.lower() if wallet else None,
            "claims": claims,
            "recommended_network": recommended,
        })
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        logger.error(f"claim_availability route error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/fv-status", methods=["GET"])
@auth_required
def fv_status():
    """Return the logged-in wallet's GoodDollar face-verification expiry data.

    Reads directly from the GoodDollar Identity contract — GoodMarket does not
    store its own FV expiry, so whatever `authenticationPeriod()` the contract
    returns (currently 180 days / 6 months) is what applies.
    """
    try:
        wallet = session.get("wallet")
        if not wallet:
            return jsonify({"success": False, "error": "not_authenticated"}), 401
        force = request.args.get("force", "0") == "1"
        from blockchain import get_identity_expiry, invalidate_fv_expiry_cache
        if force:
            invalidate_fv_expiry_cache(wallet)
        result = get_identity_expiry(wallet)
        response = jsonify(result)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, max-age=0, private"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response
    except Exception as e:
        logger.error(f"FV status route error: {e}")
        return jsonify({
            "success": False,
            "error": str(e),
            "verified": False,
            "expired": False,
            "days_remaining": 0,
        }), 500


@routes.route("/wallet")
def wallet_page():
    """Wallet page for sending/receiving G$ and CELO"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect(url_for("routes.index"))
    buy_eth_visible = True
    try:
        supabase = get_supabase_client()
        if supabase:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .select('feature_name,is_maintenance')
                    .in_('feature_name', ['wallet_feature', 'wallet_buy_eth'])
                    .execute(),
                operation_name="check wallet feature visibility"
            )
            if result and result.data:
                for row in result.data:
                    fn = row.get('feature_name')
                    if fn == 'wallet_feature' and row.get('is_maintenance', False):
                        return render_template("feature_unavailable.html", feature_name="Wallet", wallet=wallet)
                    if fn == 'wallet_buy_eth' and row.get('is_maintenance', False):
                        buy_eth_visible = False
    except Exception:
        pass
    return render_template(
        "wallet.html",
        wallet=wallet,
        login_method=session.get("login_method", "walletconnect"),
        walletconnect_project_id=os.environ.get("WALLETCONNECT_PROJECT_ID", ""),
        walletconnect_sidecar_enabled=_is_walletconnect_sidecar_enabled(),
        buy_eth_visible=buy_eth_visible,
    )


@routes.route("/swap")
def swap_page():
    """Swap page: DEX (Uniswap V3 on Celo) and GoodReserve (Mento) tabs"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect(url_for("routes.index"))
    reserve_visible = False
    try:
        supabase = get_supabase_client()
        if supabase:
            result = safe_supabase_operation(
                lambda: supabase.table('maintenance_settings')
                    .select('feature_name,is_maintenance')
                    .in_('feature_name', ['swap_feature', 'reserve_swap_feature'])
                    .execute(),
                operation_name="check swap feature visibility"
            )
            if result and result.data:
                for row in result.data:
                    if row.get('feature_name') == 'swap_feature' and row.get('is_maintenance', False):
                        return render_template("feature_unavailable.html", feature_name="Swap", wallet=wallet)
                    if row.get('feature_name') == 'reserve_swap_feature' and not row.get('is_maintenance', False):
                        reserve_visible = True
    except Exception:
        pass
    # MiniPay users now have full access to BOTH the Uniswap V3 and GoodReserve
    # swap modes (the Uniswap path uses CIP-64 fee abstraction so MiniPay can
    # pay gas in cUSD/USDT/USDC). We still ensure the reserve pane renders so
    # MiniPay always has at least the no-slippage reserve fallback available.
    ua = (request.headers.get("User-Agent") or "").lower()
    is_minipay = "minipay" in ua
    if is_minipay:
        reserve_visible = True

    # GoodDollar MessagePassingBridge — same address on every supported chain
    # per https://docs.gooddollar.org/user-guides/bridge-gooddollars
    bridge_contract = os.getenv("XDC_CELO_BRIDGE_CONTRACT", "0xa3247276DbCC76Dd7705273f766eB3E8a5ecF4a5")
    celo_gd_token_contract = os.getenv("CELO_GD_TOKEN_CONTRACT", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A")
    # XDC G$ contract is needed by the unified Bridge pane so the XDC → Celo
    # sub-tab can read the user's G$-on-XDC balance + allowance directly,
    # without bouncing the user to /xdc-wallet first.
    xdc_gd_token_contract = os.getenv("XDC_GD_TOKEN_CONTRACT", "0xEC2136843a983885AebF2feB3931F73A8eBEe50c")
    xdc_chain_id = int(os.getenv("XDC_MAINNET_CHAIN_ID", "50"))
    celo_chain_id = int(os.getenv("CELO_MAINNET_CHAIN_ID", "42220"))
    fuse_chain_id = int(os.getenv("FUSE_CHAIN_ID", "122"))
    fuse_rpc_url = os.getenv("FUSE_RPC_URL", "https://rpc.fuse.io")
    fuse_gd_token_contract = os.getenv("FUSE_GD_TOKEN", "0x495d133B938596C9984d462F007B676bDc57eCEC")
    fuse_gd_decimals = int(os.getenv("FUSE_GD_DECIMALS", "2"))
    fuse_wfuse_contract = os.getenv("FUSE_WFUSE_TOKEN", "0x0BE9e53fd7EDaC9F859882AfdDa116645287C629")
    voltage_router_contract = os.getenv("VOLTAGE_ROUTER", "0xE3F85aAd0c8DD7337427B9dF5d0fB741d65EEEB5")

    # Squid Router Celo -> Base ETH widget configuration.  These values have
    # production-safe defaults, so Vercel does not need extra Squid env vars just
    # to render the widget.  Keep them overrideable so production can add an
    # official integrator ID or change Squid endpoints/tokens without editing the
    # template.  Source token defaults are the Celo assets requested for the first
    # release; the destination is native ETH on Base using Squid's canonical
    # native-token placeholder.
    squid_integrator_id = os.getenv("SQUID_INTEGRATOR_ID", "")
    squid_api_url = os.getenv("SQUID_API_URL", "https://apiplus.squidrouter.com").rstrip("/")
    squid_iframe_base_url = os.getenv("SQUID_WIDGET_IFRAME_URL", "https://studio.squidrouter.com/iframe")
    squid_app_url = os.getenv("SQUID_APP_URL", "https://app.squidrouter.com/")
    squid_from_chain_id = int(os.getenv("SQUID_FROM_CHAIN_ID", str(celo_chain_id)))
    squid_to_chain_id = int(os.getenv("SQUID_TO_CHAIN_ID", "8453"))
    squid_to_token = os.getenv("SQUID_TO_TOKEN", "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE")
    squid_source_tokens = [
        {
            "symbol": "CELO",
            "name": "Celo",
            "address": os.getenv("SQUID_CELO_TOKEN", "0xEeeeeEeeeEeEeeEeEeEeeEEEeeeeEeeeeeeeEEeE"),
            "note": "Native gas token on Celo",
        },
        {
            "symbol": "cUSD",
            "name": "Celo Dollar",
            "address": os.getenv("SQUID_CUSD_TOKEN", "0x765DE816845861e75A25fCA122bb6898B8B1282a"),
            "note": "Celo-native stablecoin",
        },
        {
            "symbol": "USDC",
            "name": "USD Coin",
            "address": os.getenv("SQUID_USDC_TOKEN", "0xcebA9300f2b948710d2653dD7B07f33A8B32118C"),
            "note": "Native Circle USDC on Celo",
        },
        {
            "symbol": "USDT",
            "name": "Tether USD",
            "address": os.getenv("SQUID_USDT_TOKEN", "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e"),
            "note": "Tether on Celo",
        },
    ]
    squid_source_chain_id = int(squid_from_chain_id)
    squid_destination_chain_id = int(squid_to_chain_id)
    squid_widget_config = {
        "apiUrl": squid_api_url,
        "themeType": "dark",
        "initialAssets": {
            "from": {
                "address": squid_source_tokens[0]["address"],
                "chainId": squid_source_chain_id,
            },
            "to": {
                "address": squid_to_token,
                "chainId": squid_destination_chain_id,
            },
        },
        "defaultTokensPerChain": [
            {"address": token["address"], "chainId": squid_source_chain_id}
            for token in squid_source_tokens
        ] + [{"address": squid_to_token, "chainId": squid_destination_chain_id}],
        "availableChains": {
            "source": [squid_source_chain_id],
            "destination": [squid_destination_chain_id],
        },
        "availableTokens": {
            "source": {
                str(squid_source_chain_id): [token["address"] for token in squid_source_tokens],
            },
            "destination": {
                str(squid_destination_chain_id): [squid_to_token],
            },
        },
    }
    if squid_integrator_id:
        squid_widget_config["integratorId"] = squid_integrator_id

    return render_template(
        "swap.html",
        wallet=wallet,
        login_method=session.get("login_method", "walletconnect"),
        walletconnect_project_id=os.environ.get("WALLETCONNECT_PROJECT_ID", ""),
        walletconnect_sidecar_enabled=_is_walletconnect_sidecar_enabled(),
        reserve_swap_visible=reserve_visible,
        is_minipay=is_minipay,
        bridge_contract=bridge_contract,
        celo_gd_token_contract=celo_gd_token_contract,
        xdc_gd_token_contract=xdc_gd_token_contract,
        xdc_chain_id=xdc_chain_id,
        celo_chain_id=celo_chain_id,
        fuse_chain_id=fuse_chain_id,
        fuse_rpc_url=fuse_rpc_url,
        fuse_gd_token_contract=fuse_gd_token_contract,
        fuse_gd_decimals=fuse_gd_decimals,
        fuse_wfuse_contract=fuse_wfuse_contract,
        voltage_router_contract=voltage_router_contract,
        squid_integrator_id=squid_integrator_id,
        squid_api_url=squid_api_url,
        squid_iframe_base_url=squid_iframe_base_url,
        squid_app_url=squid_app_url,
        squid_widget_config=squid_widget_config,
        squid_from_chain_id=squid_from_chain_id,
        squid_to_chain_id=squid_to_chain_id,
        squid_to_token=squid_to_token,
        squid_source_tokens=squid_source_tokens,
    )


# ── GoodReserve (Mento) constants on Celo mainnet ──────────────────────────
# Verified from @gooddollar/goodprotocol/releases/deployment.json (production-celo)
# and live calls against forno.celo.org.
GOODRESERVE_BROKER_CELO   = "0x88de45906D4F5a57315c133620cfa484cB297541"
GOODRESERVE_PROVIDER_CELO = "0x2fFBB49055d487DdBBb0C052Cd7c2a02A7971e41"
GOODRESERVE_GD_CELO       = "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"
GOODRESERVE_CUSD_CELO     = "0x765DE816845861e75A25fCA122bb6898B8B1282a"
GOODRESERVE_RPC_CELO      = os.environ.get("CELO_RPC_URL", "https://forno.celo.org")

_goodreserve_quote_cache = {"data": {}, "expires": {}}
_GOODRESERVE_QUOTE_TTL   = 6  # seconds


def _goodreserve_eth_call(to_addr, data_hex):
    """Minimal eth_call helper for GoodReserve quotes (no web3 dep required)."""
    import requests
    payload = {
        "jsonrpc": "2.0", "id": 1, "method": "eth_call",
        "params": [{"to": to_addr, "data": data_hex}, "latest"],
    }
    resp = requests.post(GOODRESERVE_RPC_CELO, json=payload, timeout=8,
                         headers={"User-Agent": "GoodMarket/1.0"})
    resp.raise_for_status()
    body = resp.json()
    if "error" in body:
        raise RuntimeError(f"eth_call reverted: {body['error'].get('message','unknown')}")
    return body.get("result", "0x")


def _goodreserve_get_exchange_id():
    """Fetch the G$/cUSD exchangeId from the MentoExchangeProvider on Celo.
    Returns a 32-byte hex string (no 0x prefix) or None on failure."""
    cached = _goodreserve_quote_cache["data"].get("exchange_id")
    if cached and time.time() < _goodreserve_quote_cache["expires"].get("exchange_id", 0):
        return cached
    raw = _goodreserve_eth_call(GOODRESERVE_PROVIDER_CELO, "0x1e2e3a6b")
    hexd = raw[2:] if raw.startswith("0x") else raw
    if len(hexd) < 64 * 4:
        return None
    arr_off = int(hexd[:64], 16) * 2
    arr_len = int(hexd[arr_off:arr_off + 64], 16)
    if arr_len < 1:
        return None
    elem_offsets_start = arr_off + 64
    rel = int(hexd[elem_offsets_start:elem_offsets_start + 64], 16) * 2
    abs_off = elem_offsets_start + rel
    exchange_id = hexd[abs_off:abs_off + 64]
    _goodreserve_quote_cache["data"]["exchange_id"] = exchange_id
    _goodreserve_quote_cache["expires"]["exchange_id"] = time.time() + 3600
    return exchange_id


def _goodreserve_get_pool(exchange_id):
    """Fetch the pool struct (returns dict with reserveRatio, exitContribution)."""
    data_hex = "0x278488a4" + exchange_id
    raw = _goodreserve_eth_call(GOODRESERVE_PROVIDER_CELO, data_hex)
    hexd = raw[2:] if raw.startswith("0x") else raw
    if len(hexd) < 64 * 6:
        return None
    return {
        "reserve_asset":      "0x" + hexd[24:64],
        "token_address":      "0x" + hexd[64 + 24:128],
        "token_supply":       int(hexd[128:192], 16),
        "reserve_balance":    int(hexd[192:256], 16),
        "reserve_ratio":      int(hexd[256:320], 16),
        "exit_contribution":  int(hexd[320:384], 16),
    }


def _goodreserve_quote(direction, amount_in_wei):
    """Quote amountOut for a given direction ('buy' = cUSD->G$, 'sell' = G$->cUSD)."""
    exchange_id = _goodreserve_get_exchange_id()
    if not exchange_id:
        raise RuntimeError("GoodReserve exchange not found on Celo")
    if direction == "buy":
        token_in, token_out = GOODRESERVE_CUSD_CELO, GOODRESERVE_GD_CELO
    else:
        token_in, token_out = GOODRESERVE_GD_CELO, GOODRESERVE_CUSD_CELO
    data = (
        "0xa20f2305"
        + GOODRESERVE_PROVIDER_CELO[2:].lower().rjust(64, "0")
        + exchange_id
        + token_in[2:].lower().rjust(64, "0")
        + token_out[2:].lower().rjust(64, "0")
        + format(amount_in_wei, "x").rjust(64, "0")
    )
    raw = _goodreserve_eth_call(GOODRESERVE_BROKER_CELO, data)
    hexd = raw[2:] if raw.startswith("0x") else raw
    if len(hexd) < 64:
        raise RuntimeError("empty quote response")
    return int(hexd[:64], 16)


@routes.route("/api/reserve/quote", methods=["POST"])
def reserve_quote():
    """Read-only quote for the GoodDollar Reserve (Mento) on Celo.

    Body: { direction: 'buy' | 'sell', amount: '<decimal string of human units>' }
    Returns: amount_in_wei, amount_out_wei, exit_contribution_bps,
             reserve_ratio_bps, exchange_id, broker, provider, gd, cusd.
    """
    try:
        body = request.get_json(silent=True) or {}
        direction = (body.get("direction") or "").strip().lower()
        amount_str = str(body.get("amount") or "").strip()
        if direction not in ("buy", "sell"):
            return jsonify({"success": False, "error": "direction must be 'buy' or 'sell'"}), 400
        try:
            amount_human = float(amount_str)
        except Exception:
            return jsonify({"success": False, "error": "invalid amount"}), 400
        if amount_human <= 0:
            return jsonify({"success": False, "error": "amount must be > 0"}), 400
        amount_in_wei = int(round(amount_human * (10 ** 18)))
        cache_key = f"{direction}:{amount_in_wei}"
        now = time.time()
        cached = _goodreserve_quote_cache["data"].get(cache_key)
        cached_exp = _goodreserve_quote_cache["expires"].get(cache_key, 0)
        if cached and now < cached_exp:
            return jsonify(cached)
        exchange_id = _goodreserve_get_exchange_id()
        if not exchange_id:
            return jsonify({"success": False, "error": "reserve unavailable"}), 503
        pool = _goodreserve_get_pool(exchange_id) or {}
        amount_out_wei = _goodreserve_quote(direction, amount_in_wei)
        exit_bps = int(round(pool.get("exit_contribution", 0) / 1e8 * 10000))
        ratio_bps = int(round(pool.get("reserve_ratio", 0) / 1e8 * 10000))
        result = {
            "success": True,
            "direction": direction,
            "amount_in_wei": str(amount_in_wei),
            "amount_out_wei": str(amount_out_wei),
            "exit_contribution_bps": exit_bps,
            "reserve_ratio_bps": ratio_bps,
            "exchange_id": "0x" + exchange_id,
            "broker": GOODRESERVE_BROKER_CELO,
            "provider": GOODRESERVE_PROVIDER_CELO,
            "gd": GOODRESERVE_GD_CELO,
            "cusd": GOODRESERVE_CUSD_CELO,
            "chain_id": 42220,
        }
        _goodreserve_quote_cache["data"][cache_key] = result
        _goodreserve_quote_cache["expires"][cache_key] = now + _GOODRESERVE_QUOTE_TTL
        return jsonify(result)
    except Exception as e:
        logger.error(f"reserve_quote error: {e}")
        return jsonify({"success": False, "error": "quote failed"}), 500


@routes.route("/send-link")
def send_link_page():
    """Send G$ via a one-time payment link (GoodDollar OneTimePayments contract)"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect(url_for("routes.index"))
    return render_template(
        "send-link.html",
        wallet=wallet,
        login_method=session.get("login_method", "walletconnect"),
        walletconnect_project_id=os.environ.get("WALLETCONNECT_PROJECT_ID", ""),
        walletconnect_sidecar_enabled=_is_walletconnect_sidecar_enabled(),
    )


@routes.route("/claim")
def claim_page():
    """Claim page for one-time payment links — no login required"""
    return render_template(
        "claim.html",
        login_method=session.get("login_method", "walletconnect"),
        wallet=session.get("wallet", "")
    )


# ── Payment Link helpers ─────────────────────────────────────────���──────────
# Payment link private keys are NOT stored server-side.
# The ephemeral key lives only in the browser (localStorage + URL hash).

@routes.route("/api/payment-links", methods=["POST"])
@auth_required
def create_payment_link():
    """Save a sent payment link to the database (no private key stored server-side)"""
    try:
        wallet = session.get("wallet")
        data = request.get_json()
        payment_id = data.get("paymentId", "").strip()
        amount     = data.get("amount", "").strip()
        tx_hash    = data.get("txHash", "").strip()

        if not payment_id or not amount:
            return jsonify({"success": False, "error": "Missing fields"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "DB unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("payment_links").insert({
                "wallet_address": wallet,
                "payment_id": payment_id,
                "amount": amount,
                "tx_hash": tx_hash,
                "status": "active"
            }).execute()
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"create_payment_link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/payment-links", methods=["GET"])
@auth_required
def list_payment_links():
    """List all payment links for the current user (newest first)"""
    try:
        wallet = session.get("wallet")
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "DB unavailable"}), 503

        result = safe_supabase_operation(
            lambda: supabase.table("payment_links")
                .select("payment_id,amount,tx_hash,status,created_at")
                .eq("wallet_address", wallet)
                .order("created_at", desc=True)
                .limit(100)
                .execute()
        )
        rows = result.data if result else []
        return jsonify({"success": True, "payments": rows})
    except Exception as e:
        logger.error(f"list_payment_links error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/payment-links/<payment_id>/key", methods=["GET"])
@auth_required
def get_payment_key(payment_id):
    """Payment link keys are no longer stored server-side — claim links exist only in the browser that created them."""
    return jsonify({"success": False, "error": "Claim link is only available in the browser where this payment was created."}), 410


@routes.route("/api/payment-links/<payment_id>", methods=["PATCH"])
@auth_required
def update_payment_link(payment_id):
    """Update status of a payment link owned by the current user"""
    try:
        wallet = session.get("wallet")
        data = request.get_json()
        status = data.get("status", "").strip()
        if status not in ("active", "claimed", "cancelled"):
            return jsonify({"success": False, "error": "Invalid status"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "DB unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("payment_links")
                .update({"status": status})
                .eq("wallet_address", wallet)
                .eq("payment_id", payment_id)
                .execute()
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"update_payment_link error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/ubi-pool-balance", methods=["GET"])
def ubi_pool_balance():
    """Get the G$ balance held in the GoodDollar UBI Pool contract (public, no auth needed)."""
    try:
        from blockchain import get_gooddollar_balance, GOODDOLLAR_CONTRACTS
        ubi_proxy = GOODDOLLAR_CONTRACTS["UBI_PROXY"]
        result = get_gooddollar_balance(ubi_proxy)
        return jsonify({
            "success": True,
            "pool_address": ubi_proxy,
            "balance": result.get("balance", 0),
            "balance_formatted": result.get("balance_formatted", "—")
        })
    except Exception as e:
        logger.error(f"ubi_pool_balance error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/wallet/balances", methods=["GET"])
@auth_required
def wallet_balances():
    """Get G$, CELO, cUSD, and USDT balances for the current user.

    Fetches all four balances and the GD/USD price in parallel via a
    ThreadPoolExecutor to keep the critical path bounded by the slowest
    single fetch instead of the sum of all four. Web3.py is sync but
    releases the GIL during HTTP I/O, so threading gives a real speedup
    on Celo-RPC roundtrips.
    """
    try:
        wallet = session.get("wallet")
        if not wallet:
            return jsonify({"success": False, "error": "no_wallet"}), 401
        from blockchain import (
            get_gooddollar_balance,
            get_celo_balance,
            get_cusd_balance,
            get_usdt_balance,
            _get_gd_usd_price,
            enrich_gd_balance_with_price,
        )
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=5) as executor:
            gd_future = executor.submit(get_gooddollar_balance, wallet, False)
            celo_future = executor.submit(get_celo_balance, wallet)
            cusd_future = executor.submit(get_cusd_balance, wallet)
            usdt_future = executor.submit(get_usdt_balance, wallet)
            price_future = executor.submit(_get_gd_usd_price)
            gd = gd_future.result()
            celo = celo_future.result()
            cusd = cusd_future.result()
            usdt = usdt_future.result()
            gd_price = price_future.result()

        gd = enrich_gd_balance_with_price(gd, gd_price)
        return jsonify({"success": True, "gd": gd, "celo": celo, "cusd": cusd, "usdt": usdt})
    except Exception as e:
        logger.error(f"wallet_balances error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/wallet/history", methods=["GET"])
@auth_required
def wallet_history():
    """Get G$ transfer history for the current user"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_wallet_transfer_history
        transfers = get_wallet_transfer_history(wallet, limit=40)
        return jsonify({"success": True, "transfers": transfers})
    except Exception as e:
        logger.error(f"wallet_history error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/wallet/transaction-history", methods=["GET"])
@auth_required
def wallet_transaction_history():
    """
    Comprehensive transaction history — G$ transfers classified as:
    claim | savings_deposit | savings_withdraw | swap | transfer_sent | transfer_received
    """
    try:
        wallet = session.get("wallet")
        limit  = min(int(request.args.get("limit", 50)), 100)
        force  = request.args.get("force", "0") == "1"
        from blockchain import get_comprehensive_tx_history
        txs = get_comprehensive_tx_history(wallet, limit=limit, force=force)
        return jsonify({"success": True, "transactions": txs, "count": len(txs)})
    except Exception as e:
        logger.error(f"wallet_transaction_history error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/wallet/prepare-send", methods=["POST"])
@auth_required
def wallet_prepare_send():
    """
    Prepare ERC-20 transfer calldata for G$ or a native CELO send.
    Returns unsigned tx parameters so the frontend can request wallet signing.
    """
    try:
        data = request.get_json()
        token = data.get("token", "GD").upper()
        to_address = data.get("to", "").strip()
        amount_str = data.get("amount", "0")

        if not to_address or not to_address.startswith("0x") or len(to_address) != 42:
            return jsonify({"success": False, "error": "Invalid recipient address"}), 400

        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError("amount must be > 0")
        except Exception:
            return jsonify({"success": False, "error": "Invalid amount"}), 400

        from web3 import Web3
        from blockchain import GOODDOLLAR_CONTRACTS, CELO_CHAIN_ID, CELO_RPC

        if token in ("GD", "G$"):
            from blockchain import prepare_gd_transfer_data
            result = prepare_gd_transfer_data(to_address, amount)
            return jsonify(result)
        elif token == "CUSD":
            from blockchain import prepare_cusd_transfer_data
            result = prepare_cusd_transfer_data(to_address, amount)
            return jsonify(result)
        elif token == "USDT":
            from blockchain import prepare_usdt_transfer_data
            result = prepare_usdt_transfer_data(to_address, amount)
            return jsonify(result)
        elif token == "CELO":
            w3 = Web3(Web3.HTTPProvider(CELO_RPC))
            to_checksum = Web3.to_checksum_address(to_address)
            amount_wei = int(amount * (10 ** 18))
            return jsonify({
                "success": True,
                "to": to_checksum,
                "data": "0x",
                "value": hex(amount_wei),
                "chain_id": CELO_CHAIN_ID,
                "token": "CELO",
                "recipient": to_checksum,
                "amount": amount,
                # MiniPay/CIP-64 hint so frontend can prioritize stablecoin gas
                # currencies when sending native CELO.
                "minipay_fee_currencies": {
                    "cusd": "0x765DE816845861e75A25fCA122bb6898B8B1282a",
                    "usdt_adapter": "0x0E2A3e05bc9A16F5292A6170456A710cb89C6f72",
                    "usdc_adapter": "0x2F25deB3848C207fc8E0c34035B3Ba7fC157602B",
                },
            })
        else:
            return jsonify({"success": False, "error": f"Unsupported token: {token}"}), 400

    except Exception as e:
        logger.error(f"wallet_prepare_send error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/xdc-wallet")
def xdc_wallet_page():
    """XDC Network wallet page"""
    wallet = session.get("wallet")
    if not wallet or not session.get("verified"):
        return redirect(url_for("routes.index"))
    login_method = session.get("login_method", "")
    use_server_signing = False
    xdc_bridge_contract = os.getenv("XDC_CELO_BRIDGE_CONTRACT", "0xa3247276DbCC76Dd7705273f766eB3E8a5ecF4a5")
    xdc_gd_token_contract = os.getenv("XDC_GD_TOKEN_CONTRACT", "0xEC2136843a983885AebF2feB3931F73A8eBEe50c")
    celo_chain_id = int(os.getenv("CELO_MAINNET_CHAIN_ID", "42220"))
    return render_template("xdc_wallet.html", wallet=wallet,
                           login_method=login_method, use_server_signing=use_server_signing,
                           xdc_bridge_contract=xdc_bridge_contract,
                           xdc_gd_token_contract=xdc_gd_token_contract,
                           celo_chain_id=celo_chain_id,
                           walletconnect_project_id=os.environ.get("WALLETCONNECT_PROJECT_ID", ""),
                           walletconnect_sidecar_enabled=_is_walletconnect_sidecar_enabled())


@routes.route("/api/xdc/balances", methods=["GET"])
@auth_required
def xdc_balances():
    """Get XDC and xUSDT balances for the current user"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_xdc_balance, get_xusdt_balance
        xdc = get_xdc_balance(wallet)
        xusdt = get_xusdt_balance(wallet)
        return jsonify({"success": True, "xdc": xdc, "xusdt": xusdt})
    except Exception as e:
        logger.error(f"xdc_balances error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/xdc/history", methods=["GET"])
@auth_required
def xdc_history():
    """Get XDC transaction history for the current user"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_xdc_transfer_history
        transfers = get_xdc_transfer_history(wallet, limit=40)
        return jsonify({"success": True, "transfers": transfers})
    except Exception as e:
        logger.error(f"xdc_history error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/xdc/gd-info", methods=["GET"])
@auth_required
def xdc_gd_info():
    """Get G$ balance + UBI entitlement + identity status on XDC Network"""
    try:
        wallet = session.get("wallet")
        from blockchain import get_xdc_gd_balance, check_xdc_ubi_entitlement, is_xdc_identity_whitelisted
        gd_bal = get_xdc_gd_balance(wallet)
        entitlement = check_xdc_ubi_entitlement(wallet)
        identity = is_xdc_identity_whitelisted(wallet)
        return jsonify({
            "success": True,
            "gd_balance": gd_bal,
            "entitlement": entitlement,
            "identity": identity,
        })
    except Exception as e:
        logger.error(f"xdc_gd_info error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/fuse/balances", methods=["GET"])
@auth_required
def fuse_balances():
    """Get native FUSE and Fuse G$ balances for the current user."""
    try:
        wallet = session.get("wallet")
        from blockchain import get_fuse_balance, get_fuse_gd_balance
        fuse = get_fuse_balance(wallet)
        gd = get_fuse_gd_balance(wallet)
        return jsonify({"success": True, "fuse": fuse, "gd_balance": gd})
    except Exception as e:
        logger.error(f"fuse_balances error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/fuse/prepare-send", methods=["POST"])
@auth_required
def fuse_prepare_send():
    """Prepare Fuse G$ or native FUSE send transaction parameters."""
    try:
        data = request.get_json()
        token = data.get("token", "FUSE_GD").upper()
        to_address = data.get("to", "").strip()
        amount_str = data.get("amount", "0")

        if not to_address or not to_address.startswith("0x") or len(to_address) != 42:
            return jsonify({"success": False, "error": "Invalid recipient address"}), 400

        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError("amount must be > 0")
        except Exception:
            return jsonify({"success": False, "error": "Invalid amount"}), 400

        if token in ("FUSE_GD", "FUSEGD"):
            from blockchain import prepare_fuse_gd_send_data
            result = prepare_fuse_gd_send_data(to_address, amount)
            result["token"] = "FUSE_GD"
            return jsonify(result)
        if token == "FUSE":
            from blockchain import prepare_fuse_send_data
            return jsonify(prepare_fuse_send_data(to_address, amount))
        return jsonify({"success": False, "error": f"Unsupported token: {token}"}), 400

    except Exception as e:
        logger.error(f"fuse_prepare_send error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/xdc/prepare-send", methods=["POST"])
@auth_required
def xdc_prepare_send():
    """Prepare XDC or xUSDT send transaction parameters"""
    try:
        data = request.get_json()
        token = data.get("token", "XDC").upper()
        to_address = data.get("to", "").strip()
        amount_str = data.get("amount", "0")

        if not to_address:
            return jsonify({"success": False, "error": "Recipient address required"}), 400

        from blockchain import _normalize_xdc_address
        norm_to = _normalize_xdc_address(to_address)
        if not norm_to.startswith("0x") or len(norm_to) != 42:
            return jsonify({"success": False, "error": "Invalid XDC/Ethereum address"}), 400

        try:
            amount = float(amount_str)
            if amount <= 0:
                raise ValueError("amount must be > 0")
        except Exception:
            return jsonify({"success": False, "error": "Invalid amount"}), 400

        if token == "XDC":
            from blockchain import prepare_xdc_send_data
            result = prepare_xdc_send_data(to_address, amount)
            return jsonify(result)
        elif token == "XUSDT":
            from blockchain import prepare_xdc_token_send_data, XUSDT_CONTRACT
            result = prepare_xdc_token_send_data(to_address, amount, XUSDT_CONTRACT, decimals=6)
            return jsonify(result)
        elif token in ("XDC_GD", "XDCGD"):
            from blockchain import prepare_xdc_token_send_data, XDC_GD_TOKEN, XDC_GD_DECIMALS
            result = prepare_xdc_token_send_data(to_address, amount, XDC_GD_TOKEN, decimals=XDC_GD_DECIMALS)
            result["token"] = "XDC_GD"
            return jsonify(result)
        else:
            return jsonify({"success": False, "error": f"Unsupported token: {token}"}), 400

    except Exception as e:
        logger.error(f"xdc_prepare_send error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/xdc/bridge/estimate-fee", methods=["GET"])
@auth_required
def xdc_bridge_estimate_fee():
    """Estimate bridge fee for XDC -> Celo G$ bridge with safe fallback."""
    try:
        source_chain_id = request.args.get("sourceChainId", "50").strip() or "50"
        target_chain_id = request.args.get("targetChainId", "42220").strip() or "42220"
        amount = request.args.get("amount", "1").strip() or "1"
        try:
            source_chain_id_val = int(source_chain_id)
            target_chain_id_val = int(target_chain_id)
            amount_val = float(amount)
            if amount_val <= 0:
                raise ValueError("amount must be > 0")
        except Exception:
            return jsonify({"success": False, "error": "Invalid chain ids or amount"}), 400

        fallback_fees = {
            (50, 42220): Decimal("1.6176"),   # XDC -> Celo (XDC native)
            (50, 122): Decimal("1.6176"),     # XDC -> Fuse (XDC native)
            (42220, 50): Decimal("0.1151"),   # Celo -> XDC (CELO native)
            (42220, 122): Decimal("0.1151"),  # Celo -> Fuse (CELO native)
            (122, 42220): Decimal("0.5"),     # Fuse -> Celo (FUSE native)
            (122, 50): Decimal("0.5"),        # Fuse -> XDC (FUSE native)
        }
        default_fee_xdc = fallback_fees.get((source_chain_id_val, target_chain_id_val), Decimal("1.6176"))
        fee_source = "fallback_default"
        bridge_fee_xdc = default_fee_xdc
        raw_payload = None

        urls = [
            # GoodDocs recommends this endpoint without query params, then pick route key.
            "https://goodserver.gooddollar.org/bridge/estimatefees",
            # Keep compatibility with older route-specific response shapes.
            (
                "https://goodserver.gooddollar.org/bridge/estimatefees"
                f"?sourceChainId={source_chain_id_val}&targetChainId={target_chain_id_val}&amount={amount}"
            ),
        ]

        chain_aliases = {
            1: "ETH",
            50: "XDC",
            122: "FUSE",
            42220: "CELO",
        }
        source_alias = chain_aliases.get(source_chain_id_val)
        target_alias = chain_aliases.get(target_chain_id_val)

        # Per https://docs.gooddollar.org/user-guides/bridge-gooddollars: the
        # canonical route keys are ``LZ_<SRC>_TO_<DST>`` (LayerZero) and
        # ``AXL_<SRC>_TO_<DST>`` (Axelar) where <SRC>/<DST> are the chain
        # aliases above. "Bridging from/to Fuse and XDC is only supported by
        # LayerZero", so for any pair involving XDC or Fuse the parser
        # restricts itself to LZ_* keys to mirror what the docs prescribe.
        if source_alias and target_alias:
            xdc_or_fuse = {"XDC", "FUSE"}
            if source_alias in xdc_or_fuse or target_alias in xdc_or_fuse:
                preferred_route_keys = (f"LZ_{source_alias}_TO_{target_alias}",)
            else:
                preferred_route_keys = (
                    f"LZ_{source_alias}_TO_{target_alias}",
                    f"AXL_{source_alias}_TO_{target_alias}",
                )
        else:
            preferred_route_keys = ()

        # Older / generic shapes some intermediate proxies might use.
        legacy_route_keys = (
            f"LZ_{source_chain_id_val}_TO_{target_chain_id_val}",
            f"{source_chain_id_val}_TO_{target_chain_id_val}",
        )
        if source_alias and target_alias:
            legacy_route_keys += (
                f"{source_alias}_TO_{target_alias}",
            )
        all_route_keys = preferred_route_keys + legacy_route_keys

        def _scan_dict_for_route_keys(scope):
            if not isinstance(scope, dict):
                return None
            for route_key in all_route_keys:
                if route_key not in scope:
                    continue
                route_val = scope.get(route_key)
                if isinstance(route_val, dict):
                    for key in ("fee", "bridgeFee", "nativeFee", "estimatedFee", "value"):
                        if key in route_val:
                            return route_val.get(key)
                elif route_val is not None:
                    return route_val
            return None

        def _extract_candidate(payload_obj):
            if not isinstance(payload_obj, dict):
                return None

            # Direct top-level fee / bridgeFee / nativeFee / estimatedFee shape.
            for key in ("fee", "bridgeFee", "nativeFee", "estimatedFee"):
                if key in payload_obj:
                    return payload_obj.get(key)

            if "data" in payload_obj and isinstance(payload_obj["data"], dict):
                for key in ("fee", "bridgeFee", "nativeFee", "estimatedFee"):
                    if key in payload_obj["data"]:
                        return payload_obj["data"].get(key)

            # Canonical goodserver shape (per GoodDocs):
            # { "LAYERZERO": { "LZ_CELO_TO_XDC": "0.115... Celo", ... },
            #   "AXELAR":    { "AXL_ETH_TO_CELO": "0.000... ETH", ... } }
            for nested_key in ("LAYERZERO", "AXELAR", "layerzero", "axelar"):
                nested = payload_obj.get(nested_key)
                hit = _scan_dict_for_route_keys(nested)
                if hit is not None:
                    return hit

            # Some proxies flatten everything to the top level.
            top_level_hit = _scan_dict_for_route_keys(payload_obj)
            if top_level_hit is not None:
                return top_level_hit

            return None

        for url in urls:
            try:
                with urllib.request.urlopen(url, timeout=8) as resp:
                    body = resp.read().decode("utf-8", errors="replace")
                    raw_payload = body[:1000]
                    payload = json.loads(body) if body else {}
                    candidate = _extract_candidate(payload)
                    if candidate is None:
                        continue
                    parsed_fee = _parse_bridge_fee_candidate_xdc(candidate)
                    if parsed_fee and parsed_fee > 0:
                        bridge_fee_xdc = parsed_fee
                        fee_source = "goodserver_estimatefees"
                        break
            except Exception as api_err:
                logger.warning(f"xdc bridge fee estimate fallback from {url}: {api_err}")

        # Add a safety buffer so UI defaults are less likely to underpay rapidly changing
        # LayerZero route requirements between estimation and submission.
        safety_multiplier = Decimal("1.25") if fee_source == "fallback_default" else Decimal("1.15")
        minimum_extra_xdc = Decimal("0.05")
        recommended_bridge_fee_xdc = max(
            bridge_fee_xdc * safety_multiplier,
            bridge_fee_xdc + minimum_extra_xdc
        )
        bridge_fee_wei = int((recommended_bridge_fee_xdc * Decimal("1e18")).to_integral_value(rounding=ROUND_CEILING))
        estimated_fee_wei = int((bridge_fee_xdc * Decimal("1e18")).to_integral_value(rounding=ROUND_CEILING))

        return jsonify({
            "success": True,
            "source_chain_id": source_chain_id_val,
            "target_chain_id": target_chain_id_val,
            "amount": amount_val,
            "bridge_fee_xdc": float(recommended_bridge_fee_xdc),
            "bridge_fee_wei": str(bridge_fee_wei),
            "estimated_bridge_fee_xdc": float(bridge_fee_xdc),
            "estimated_bridge_fee_wei": str(estimated_fee_wei),
            "recommended_bridge_fee_xdc": float(recommended_bridge_fee_xdc),
            "recommended_bridge_fee_wei": str(bridge_fee_wei),
            "source": fee_source,
            "raw_payload_preview": raw_payload if fee_source != "goodserver_estimatefees" else None,
        })
    except Exception as e:
        logger.error(f"xdc_bridge_estimate_fee error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/xdc/bridge/debug-log", methods=["POST"])
@auth_required
def xdc_bridge_debug_log():
    """Temporary bridge diagnostics logging for XDC->Celo failures."""
    try:
        wallet = session.get("wallet")
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "Invalid payload"}), 400

        # Keep logs compact and safe.
        event = str(payload.get("event", "unknown"))[:64]
        attempt_id = str(payload.get("attempt_id", "n/a"))[:64]
        details = payload.get("details")
        if details is not None:
            try:
                details = json.dumps(details, ensure_ascii=False)[:2000]
            except Exception:
                details = str(details)[:2000]

        logger.warning(
            "xdc_bridge_debug wallet=%s event=%s attempt_id=%s details=%s",
            wallet,
            event,
            attempt_id,
            details,
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"xdc_bridge_debug_log error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/walletconnect/frontend-log", methods=["POST"])
def walletconnect_frontend_log():
    """Lightweight diagnostics endpoint for homepage WalletConnect failures."""
    try:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"success": False, "error": "Invalid payload"}), 400

        event = str(payload.get("event", "unknown"))[:64]
        stage = str(payload.get("stage", "n/a"))[:64]
        message = str(payload.get("message", ""))[:500]
        user_agent = str(payload.get("user_agent", ""))[:300]
        href = str(payload.get("href", ""))[:300]
        relay_host = str(payload.get("relay_host", ""))[:120]

        logger.warning(
            "walletconnect_frontend_log event=%s stage=%s message=%s relay_host=%s href=%s ua=%s",
            event,
            stage,
            message,
            relay_host,
            href,
            user_agent,
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"walletconnect_frontend_log error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



def _get_fernet():
    """Return a Fernet instance keyed from SESSION_SECRET using PBKDF2 (stronger than SHA-256)."""
    import hashlib, base64
    from cryptography.fernet import Fernet
    secret = os.environ.get("SESSION_SECRET", "goodmarket-default-secret")
    salt = b"goodmarket-session-v2"
    key_bytes = hashlib.pbkdf2_hmac("sha256", secret.encode(), salt, iterations=200_000)
    fernet_key = base64.urlsafe_b64encode(key_bytes)
    return Fernet(fernet_key)

@routes.route("/api/walletconnect-disabled/<path:_path>", methods=["GET", "POST"])
def walletconnect_disabled(_path):
    return jsonify({
        "success": False,
        "error": "Server-side wallet management was removed. Please use WalletConnect signing from your own wallet app."
    }), 410


@routes.route("/api/notifications", methods=["GET"])
def get_notifications():
    """Return user notifications."""
    try:
        wallet = session.get("wallet")
        if not wallet:
            return json.dumps({"success": False, "message": "Not authenticated"}), 401, {"Content-Type": "application/json"}

        limit = int(request.args.get("limit", 50))
        result = notification_service.get_all_notifications(wallet, limit)

        notifications = result.get("notifications", [])
        has_broadcast = any(n.get("type") == "admin_broadcast" for n in notifications)

        return json.dumps({
            "success": True,
            "notifications": notifications,
            "unread_count": result.get("unread_count", 0),
            "total_count": result.get("total_count", 0),
            "has_broadcast": has_broadcast
        }), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error fetching notifications: {e}")
        return json.dumps({"success": False, "message": "Server error"}), 500, {"Content-Type": "application/json"}


@routes.route("/api/notifications/mark-read", methods=["POST"])
def mark_notifications_read():
    """Mark notifications as read."""
    try:
        wallet = session.get("wallet")
        if not wallet:
            return json.dumps({"success": False, "message": "Not authenticated"}), 401, {"Content-Type": "application/json"}

        data = request.get_json() or {}
        notification_ids = data.get("notification_ids", [])
        result = notification_service.mark_notifications_read(wallet, notification_ids)
        return json.dumps(result), 200, {"Content-Type": "application/json"}
    except Exception as e:
        logger.error(f"Error marking notifications read: {e}")
        return json.dumps({"success": False, "message": "Server error"}), 500, {"Content-Type": "application/json"}


# ─────────────────────────────────────────────────────────
#  DAILY VOUCHER
# ────────────────────────────────────��────────────────────

def _get_today_pht():
    """Return the current date string (YYYY-MM-DD) in PHT (UTC+8)."""
    from datetime import datetime, timezone, timedelta
    pht = timezone(timedelta(hours=8))
    return datetime.now(pht).strftime("%Y-%m-%d")


@routes.route("/api/voucher/daily", methods=["GET"])
@auth_required
def get_daily_voucher():
    """Return the active daily voucher for today if not yet claimed."""
    try:
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": True, "voucher": None, "reason": "db_unavailable"})

        result = safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .select("id, voucher_link, is_claimed, claimed_at, voucher_date")
                .eq("voucher_date", today)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="get daily voucher"
        )

        if not result or not result.data:
            return jsonify({"success": True, "voucher": None, "reason": "no_voucher_today"})

        row = result.data[0]
        if row.get("is_claimed"):
            return jsonify({"success": True, "voucher": None, "reason": "already_claimed"})

        return jsonify({
            "success": True,
            "voucher": {
                "id": row["id"],
                "voucher_link": row["voucher_link"],
                "voucher_date": row["voucher_date"],
            }
        })
    except Exception as e:
        logger.error(f"get_daily_voucher error: {e}")
        return jsonify({"success": False, "voucher": None, "error": str(e)}), 500


@routes.route("/api/voucher/claim", methods=["POST"])
@auth_required
def claim_daily_voucher():
    """Mark today's voucher as claimed. First user to call this wins."""
    try:
        from datetime import datetime, timezone
        wallet = session.get("wallet")
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        result = safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .select("id, is_claimed, voucher_link")
                .eq("voucher_date", today)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="fetch voucher for claim"
        )

        if not result or not result.data:
            return jsonify({"success": False, "error": "No voucher available today."}), 404

        row = result.data[0]
        if row.get("is_claimed"):
            return jsonify({"success": False, "error": "Voucher already claimed!", "already_claimed": True}), 409

        safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .update({
                    "is_claimed": True,
                    "claimed_by": wallet,
                    "claimed_at": datetime.now(timezone.utc).isoformat()
                })
                .eq("id", row["id"])
                .eq("is_claimed", False)
                .execute(),
            operation_name="mark voucher claimed"
        )

        return jsonify({"success": True, "voucher_link": row["voucher_link"]})
    except Exception as e:
        logger.error(f"claim_daily_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/voucher/confirm", methods=["POST"])
@auth_required
def confirm_voucher_claim():
    """Save the on-chain tx_hash and G$ amount after a successful voucher claim."""
    try:
        from datetime import datetime, timezone
        wallet = session.get("wallet")
        data = request.get_json() or {}
        tx_hash = (data.get("tx_hash") or "").strip()
        gd_amount = float(data.get("gd_amount") or 0)
        voucher_date = (data.get("voucher_date") or _get_today_pht()).strip()

        if not tx_hash:
            return jsonify({"success": False, "error": "tx_hash is required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("voucher_claims_log")
                .insert({
                    "wallet_address": wallet,
                    "voucher_date": voucher_date,
                    "tx_hash": tx_hash,
                    "gd_amount": gd_amount,
                    "claimed_at": datetime.now(timezone.utc).isoformat()
                })
                .execute(),
            operation_name="insert voucher claim log"
        )
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"confirm_voucher_claim error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/voucher", methods=["POST"])
@admin_required
def admin_set_voucher():
    """Admin: set or update today's daily voucher link."""
    try:
        wallet = session.get("wallet")
        data = request.get_json()
        voucher_link = (data.get("voucher_link") or "").strip()
        voucher_date = (data.get("voucher_date") or _get_today_pht()).strip()

        if not voucher_link:
            return jsonify({"success": False, "error": "voucher_link is required"}), 400

        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        existing = safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .select("id")
                .eq("voucher_date", voucher_date)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="check existing voucher"
        )

        if existing and existing.data:
            safe_supabase_operation(
                lambda: supabase.table("daily_voucher")
                    .update({"voucher_link": voucher_link, "is_claimed": False, "claimed_by": None, "claimed_at": None, "created_by": wallet})
                    .eq("voucher_date", voucher_date)
                    .execute(),
                operation_name="update voucher link"
            )
        else:
            safe_supabase_operation(
                lambda: supabase.table("daily_voucher")
                    .insert({"voucher_date": voucher_date, "voucher_link": voucher_link, "is_claimed": False, "created_by": wallet})
                    .execute(),
                operation_name="insert voucher"
            )

        log_admin_action(wallet, "set_daily_voucher", {"voucher_date": voucher_date, "voucher_link": voucher_link})
        return jsonify({"success": True, "voucher_date": voucher_date})
    except Exception as e:
        logger.error(f"admin_set_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/voucher", methods=["GET"])
@admin_required
def admin_get_voucher():
    """Admin: get current voucher status for today."""
    try:
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        result = safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .select("id, voucher_date, voucher_link, is_claimed, claimed_by, claimed_at, created_by")
                .eq("voucher_date", today)
                .limit(1)
                .execute(),
            fallback_result=type("obj", (object,), {"data": []})(),
            operation_name="admin get voucher"
        )

        if not result or not result.data:
            return jsonify({"success": True, "voucher": None, "today": today})

        return jsonify({"success": True, "voucher": result.data[0], "today": today})
    except Exception as e:
        logger.error(f"admin_get_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/voucher/delete", methods=["POST"])
@admin_required
def admin_delete_voucher():
    """Admin: completely delete today's voucher so it no longer shows on any dashboard."""
    try:
        wallet = session.get("wallet")
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .delete()
                .eq("voucher_date", today)
                .execute(),
            operation_name="delete voucher"
        )

        log_admin_action(wallet, "delete_daily_voucher", {"voucher_date": today})
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"admin_delete_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/admin/voucher/reset", methods=["POST"])
@admin_required
def admin_reset_voucher():
    """Admin: reset today's voucher claim status so it becomes available again."""
    try:
        wallet = session.get("wallet")
        today = _get_today_pht()
        supabase = get_supabase_client()
        if not supabase:
            return jsonify({"success": False, "error": "Database unavailable"}), 503

        safe_supabase_operation(
            lambda: supabase.table("daily_voucher")
                .update({"is_claimed": False, "claimed_by": None, "claimed_at": None})
                .eq("voucher_date", today)
                .execute(),
            operation_name="reset voucher"
        )

        log_admin_action(wallet, "reset_daily_voucher", {"voucher_date": today})
        return jsonify({"success": True})
    except Exception as e:
        logger.error(f"admin_reset_voucher error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── Unified Treasury Routes ────────────────────────────────────────────────────

@routes.route("/api/admin/treasury/status", methods=["GET"])
@admin_required
def admin_treasury_status():
    """Return current Unified Treasury balance, stats, and recipient addresses."""
    try:
        from unified_treasury.service import get_treasury_status
        status = get_treasury_status()
        return jsonify(status)
    except Exception as e:
        logger.error(f"admin_treasury_status error: {e}")
        return jsonify({"configured": False, "error": str(e)}), 500


@routes.route("/api/admin/treasury/distribute", methods=["POST"])
@admin_required
def admin_treasury_distribute():
    """
    Distribute G$ from the Unified Treasury to a hardcoded recipient.
    Body: { "recipient_key": "learn_earn"|"daily_task"|"discourse"|
                             "minigames"|"community_stories"|"referral",
            "amount": <float G$> }
    """
    try:
        from unified_treasury.service import distribute_funds, RECIPIENT_LABELS
        wallet = session.get("wallet")
        data   = request.get_json(force=True) or {}

        recipient_key = data.get("recipient_key", "").strip()
        amount        = float(data.get("amount", 0))

        if not recipient_key:
            return jsonify({"success": False, "error": "recipient_key is required"}), 400
        if recipient_key not in RECIPIENT_LABELS:
            return jsonify({"success": False, "error": f"Unknown recipient: {recipient_key}"}), 400
        if amount <= 0:
            return jsonify({"success": False, "error": "Amount must be greater than 0"}), 400

        result = distribute_funds(recipient_key, amount)

        if result.get("success"):
            log_admin_action(wallet, "treasury_distribute", {
                "recipient_key":   recipient_key,
                "recipient_label": RECIPIENT_LABELS.get(recipient_key),
                "amount_gd":       amount,
                "tx_hash":         result.get("tx_hash"),
            })

        return jsonify(result)
    except Exception as e:
        logger.error(f"admin_treasury_distribute error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


# ── UBI Gas Faucet (safe claim flow support) ─────────────────────────────────
# Short-term anti-duplicate cache: wallet_address -> unix timestamp of last
# successful refill request (API or on-chain).
_faucet_recent_refill: dict = {}
_faucet_api_pending: dict = {}
_minipay_cusd_recent_refill: dict = {}
# Track force_onchain attempts: wallet_address -> list of timestamps
_force_onchain_attempts: dict = {}
_faucet_lock = threading.Lock()

# Gas threshold for the unified claim flow. This value is the *minimum floor*
# for the readiness check — the actual `required_gas_wei` is computed
# dynamically as estimated_gas * gas_price * FAUCET_BUFFER_MULTIPLIER and
# floored at FAUCET_MIN_CELO. See _get_gas_status() for the full logic.
#   - If wallet has >= max(dynamic_required, FAUCET_MIN_CELO) -> gas_ready=True.
#   - Otherwise -> call GoodDollar API faucet (Step B), then TOPWALLET_KEY on-chain
#     fallback (Step C).
# The default floor is 0.1 CELO. A lower 0.005 default was too low: when
# Celo's RPC reports a typical gas price (5–15 gwei), the dynamic component
# evaluates to ~0.003 CELO and the max() picks the floor — so a wallet with
# e.g. 0.06 CELO was marked gas_ready=True even though the actual claim
# transaction (especially over WalletConnect / Trust Wallet, where the wallet
# adds its own priority fee on top of forno's base price) consumes ~0.07–0.1
# CELO during real congestion. Raising the floor to 0.1 CELO ensures the
# faucet is requested for any wallet below typical end-to-end claim cost,
# matching what MetaMask injected used to catch via the wallet's own higher
# eth_gasPrice. The dynamic component still overrides the floor during real
# congestion (e.g. 200+ gwei → 0.06 CELO+ required) so spike-time claims still
# trigger faucet requests at higher balances. Operators can override via the
# FAUCET_MIN_CELO env var.
FAUCET_MIN_CELO = float(os.getenv("FAUCET_MIN_CELO", "0.1"))
FAUCET_MIN_XDC = float(os.getenv("FAUCET_MIN_XDC", "0.003"))
FAUCET_MIN_FUSE = float(os.getenv("FAUCET_MIN_FUSE", "0.003"))
FAUCET_BUFFER_MULTIPLIER = float(os.getenv("FAUCET_BUFFER_MULTIPLIER", "1.35"))
FAUCET_DUPLICATE_WINDOW_MIN = int(os.getenv("FAUCET_DUPLICATE_WINDOW_MIN", "30"))
FAUCET_API_GRACE_SECONDS = int(os.getenv("FAUCET_API_GRACE_SECONDS", "30"))
FAUCET_PENDING_TTL_SECONDS = int(os.getenv("FAUCET_PENDING_TTL_SECONDS", "180"))
FAUCET_ONCHAIN_MAX_ATTEMPTS = max(1, int(os.getenv("FAUCET_ONCHAIN_MAX_ATTEMPTS", "3")))
FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR = int(os.getenv("FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR", "2"))
FAUCET_FORCE_ONCHAIN_HOUR_WINDOW = 3600  # 1 hour in seconds
# Faucet amount tuned to MiniPay's stablecoin fee-currency flow. MiniPay
# does not accept native CELO as gas, so a CELO-only wallet needs cUSD before
# it can even sign the CELO -> cUSD swap. That path can require two user txs
# (approve + swap), not just one claim(), so the default must cover a small
# two-transaction gas budget while keeping faucet spend bounded. Operators can
# override via env if Celo gas conditions change.
MINIPAY_CUSD_FAUCET_AMOUNT = Decimal(os.getenv("MINIPAY_CUSD_FAUCET_AMOUNT", "0.05"))
MINIPAY_CUSD_FAUCET_PROGRAM_LABEL = "Program by Betz & Omar Team"
# Threshold below which we treat the user as needing a stablecoin gas top-up.
# Must be <= MINIPAY_CUSD_FAUCET_AMOUNT so the user graduates to "stable_ready"
# after a single faucet refill (otherwise they remain eligible forever and
# only the cooldown gates further refills).
# Keep in sync with static/js/minipay-gas-topup.js STABLECOIN_GAS_MIN_USD.
# 0.01 can pass pre-check but still fail approve+claim due to fee volatility.
MINIPAY_STABLECOIN_MIN_USD = Decimal(os.getenv("MINIPAY_STABLECOIN_MIN_USD", "0.02"))
# Per-wallet cooldown between successful refills. 48h matches our retention
# expectation: a fresh MiniPay user who claims today should not be eligible
# again until they actually return tomorrow + buffer.
MINIPAY_CUSD_FAUCET_COOLDOWN_SECONDS = int(os.getenv("MINIPAY_CUSD_FAUCET_COOLDOWN_SECONDS", "172800"))
MINIPAY_CUSD_FAUCET_RECEIPT_TIMEOUT = int(os.getenv("MINIPAY_CUSD_FAUCET_RECEIPT_TIMEOUT", "120"))
# Per-wallet GoodDollar Celo gas faucet cooldown. The GoodDollar topWallet
# API (and the TOPWALLET_KEY on-chain fallback that calls the same faucet
# contract) hand out ~0.3 CELO per refill, which covers ~3 days of normal
# claims at typical Celo gas prices. We persist the timestamp of every
# successful refill in the celo_gas_faucet_refills table so the cooldown
# survives restarts and works across multiple workers, and we block both
# the API path and force_onchain fallback for the duration. Operators can
# tune via env; the default is 48h, slightly under the 3-day coverage so
# users who actually claim daily aren't bricked but spending the gas on
# unrelated transfers will still cost them 48h before another refill.
CELO_GAS_FAUCET_GOODDOLLAR_COOLDOWN_SECONDS = int(
    os.getenv("CELO_GAS_FAUCET_GOODDOLLAR_COOLDOWN_SECONDS", "172800")
)
CELO_GAS_FAUCET_COVERAGE_MESSAGE = (
    "You just received ~0.3 CELO of gas from the GoodDollar faucet. This is "
    "intended to cover roughly 3 days of claims. Please don't transfer this "
    "CELO out — if it's spent or moved, your next claim will fail and "
    "GoodMarket can't request more gas for you for 48 hours. You can always "
    "top up CELO yourself if you need to send transactions sooner."
)
MINIPAY_CUSD_CONTRACT = os.getenv("CUSD_CONTRACT", "0x765DE816845861e75A25fCA122bb6898B8B1282a")
MINIPAY_USDT_CONTRACT = os.getenv("USDT_CONTRACT", "0x48065fbBE25f71C9282ddf5e1cD6D6A887483D5e")
MINIPAY_USDC_CONTRACT = os.getenv("USDC_CONTRACT", "0xcebA9300f2b948710d2653dD7B07f33A8B32118C")
_MINIPAY_ERC20_ABI = [
    {"constant": True, "inputs": [{"name": "account", "type": "address"}], "name": "balanceOf", "outputs": [{"name": "", "type": "uint256"}], "type": "function"},
    {"constant": False, "inputs": [{"name": "to", "type": "address"}, {"name": "amount", "type": "uint256"}], "name": "transfer", "outputs": [{"name": "", "type": "bool"}], "type": "function"},
]
GOODDOLLAR_FAUCET_CONTRACT = os.getenv(
    "GOODDOLLAR_FAUCET_CONTRACT",
    "0x4F93Fa058b03953C851eFaA2e4FC5C34afDFAb84"
)
GOODDOLLAR_FAUCET_API_URL = os.getenv(
    "GOODDOLLAR_FAUCET_API_URL",
    "https://goodserver.gooddollar.org/verify/topWallet"
)
GOODDOLLAR_XDC_FAUCET_API_URL = os.getenv(
    "GOODDOLLAR_XDC_FAUCET_API_URL",
    "https://goodserver.gooddollar.org/verify/topWallet"
)
GOODDOLLAR_XDC_FAUCET_CONTRACT = os.getenv(
    "GOODDOLLAR_XDC_FAUCET_CONTRACT",
    "0x7344Da1Be296f03fbb8082aDaC5696058B5a9bd9"
)
GOODDOLLAR_FUSE_FAUCET_API_URL = os.getenv(
    "GOODDOLLAR_FUSE_FAUCET_API_URL",
    "https://goodserver.gooddollar.org/verify/topWallet"
)
GOODDOLLAR_FUSE_FAUCET_CONTRACT = os.getenv(
    "GOODDOLLAR_FUSE_FAUCET_CONTRACT",
    "0x01ab5966C1d742Ae0CFF7f14cC0F4D85156e83d9"
)


def _validate_and_authorize_wallet(data: dict) -> tuple:
    """Validate requested wallet and ensure it belongs to current session."""
    wallet = session.get("wallet")
    if not wallet:
        return None, jsonify({"success": False, "error": "Not logged in"}), 401

    requested_wallet = (data.get("wallet") or wallet).strip()
    if requested_wallet.lower() != wallet.lower():
        return None, jsonify({
            "success": False,
            "error": "Wrong wallet connected. Please use your logged-in wallet."
        }), 403

    try:
        checksum_wallet = Web3.to_checksum_address(requested_wallet)
    except Exception:
        return None, jsonify({"success": False, "error": "Invalid wallet address"}), 400

    return checksum_wallet, None, None


def _get_gas_status(w3, checksum_wallet: str) -> dict:
    """Compute Celo gas requirement dynamically with a safety floor.

    Behavior (aligned with the XDC sibling _get_xdc_gas_status):
      - dynamic_required = estimated_gas * gas_price * FAUCET_BUFFER_MULTIPLIER
      - required = max(dynamic_required, FAUCET_MIN_CELO)
      - balance >= required  -> gas_ready=True, claim proceeds.
      - balance <  required  -> caller should request faucet (GoodDollar API first,
                                TOPWALLET_KEY on-chain fallback only if API is down/failed).

    Why dynamic instead of a fixed flat floor: Celo gas can spike well above
    a low flat floor during peak congestion (observed 0.07+ CELO needed). A
    too-low flat floor caused wallets with sufficient absolute balance
    (e.g. 0.05–0.06 CELO) to be marked gas_ready=True and proceed to a claim
    that then reverted on insufficient funds, because the faucet was never
    requested. Using dynamic estimate + buffer is meant to match what the
    on-chain tx actually charges.

    Why we still preserve a floor: forno's eth_gasPrice often reports the
    chain's base fee only (~5–15 gwei) rather than the higher effective price
    wallets actually use, so the dynamic component can underestimate by an
    order of magnitude. The FAUCET_MIN_CELO floor (default 0.1 CELO) acts as
    a safety net for that under-reporting and matches typical end-to-end
    claim cost during congestion. The dynamic max() override still kicks in
    during real spikes so high-congestion claims trigger faucet requests at
    higher balances too.
    """
    from blockchain import GOODDOLLAR_CONTRACTS

    claim_selector = "0x4e71d92d"  # claim()
    try:
        estimated_gas = w3.eth.estimate_gas({
            "from": checksum_wallet,
            "to": Web3.to_checksum_address(GOODDOLLAR_CONTRACTS["UBI_PROXY"]),
            "data": claim_selector,
            "value": 0,
        })
    except Exception:
        # estimate_gas can revert when the user has already claimed today or
        # for any non-gas precondition. Fall back to a reasonable claim() gas
        # so we still produce a sane required threshold for the readiness check.
        estimated_gas = 220000

    try:
        gas_price_wei = int(w3.eth.gas_price)
    except Exception:
        # Fallback gas price ~50 gwei if the RPC fails to report.
        gas_price_wei = int(w3.to_wei(50, "gwei"))

    # Dynamic requirement: estimated_gas * gas_price * buffer (1.35x default).
    dynamic_required_wei = int(estimated_gas * gas_price_wei * FAUCET_BUFFER_MULTIPLIER)

    # Safety floor: never below FAUCET_MIN_CELO, never above what the network
    # actually needs. Whichever is higher wins.
    minimum_wei = int(w3.to_wei(FAUCET_MIN_CELO, "ether"))
    required_wei = max(dynamic_required_wei, minimum_wei)

    balance_wei = int(w3.eth.get_balance(checksum_wallet))
    required_celo = float(w3.from_wei(required_wei, "ether"))

    return {
        "balance_wei": str(balance_wei),
        "balance_celo": float(w3.from_wei(balance_wei, "ether")),
        "estimated_gas": int(estimated_gas),
        "gas_price_wei": str(gas_price_wei),
        "required_gas_wei": str(required_wei),
        "required_gas_celo": required_celo,
        "gas_threshold_celo": required_celo,
        "gas_ready": balance_wei >= required_wei,
    }


def _get_xdc_gas_status(w3, checksum_wallet: str) -> dict:
    """Estimate XDC claim gas reserve and compare with current XDC balance."""
    from blockchain import XDC_UBI_SCHEME

    claim_selector = "0x4e71d92d"  # claim()
    try:
        estimated_gas = w3.eth.estimate_gas({
            "from": checksum_wallet,
            "to": Web3.to_checksum_address(XDC_UBI_SCHEME),
            "data": claim_selector,
            "value": 0,
        })
    except Exception:
        estimated_gas = 220000

    gas_price_wei = int(w3.eth.gas_price)
    required_wei = int(estimated_gas * gas_price_wei * FAUCET_BUFFER_MULTIPLIER)
    minimum_wei = w3.to_wei(FAUCET_MIN_XDC, "ether")
    required_wei = max(required_wei, int(minimum_wei))

    balance_wei = int(w3.eth.get_balance(checksum_wallet))
    required_xdc = float(w3.from_wei(required_wei, "ether"))
    balance_xdc = float(w3.from_wei(balance_wei, "ether"))
    return {
        "balance_wei": str(balance_wei),
        "balance_xdc": balance_xdc,
        "estimated_gas": int(estimated_gas),
        "gas_price_wei": str(gas_price_wei),
        "required_gas_wei": str(required_wei),
        "required_gas_xdc": required_xdc,
        "required_gas_celo": required_xdc,  # compatibility for shared FE parsers
        "gas_ready": balance_wei >= required_wei,
    }



def _get_fuse_gas_status(w3, checksum_wallet: str) -> dict:
    """Estimate Fuse claim gas reserve and compare with current FUSE balance."""
    from blockchain import FUSE_UBI_SCHEME

    claim_selector = "0x4e71d92d"  # claim()
    try:
        estimated_gas = w3.eth.estimate_gas({
            "from": checksum_wallet,
            "to": Web3.to_checksum_address(FUSE_UBI_SCHEME),
            "data": claim_selector,
            "value": 0,
        })
    except Exception:
        estimated_gas = 220000

    try:
        gas_price_wei = int(w3.eth.gas_price)
    except Exception:
        gas_price_wei = int(w3.to_wei(20, "gwei"))

    required_wei = int(estimated_gas * gas_price_wei * FAUCET_BUFFER_MULTIPLIER)
    minimum_wei = w3.to_wei(FAUCET_MIN_FUSE, "ether")
    required_wei = max(required_wei, int(minimum_wei))

    balance_wei = int(w3.eth.get_balance(checksum_wallet))
    required_fuse = float(w3.from_wei(required_wei, "ether"))
    balance_fuse = float(w3.from_wei(balance_wei, "ether"))
    return {
        "balance_wei": str(balance_wei),
        "balance_fuse": balance_fuse,
        "estimated_gas": int(estimated_gas),
        "gas_price_wei": str(gas_price_wei),
        "required_gas_wei": str(required_wei),
        "required_gas_fuse": required_fuse,
        "required_gas_xdc": required_fuse,  # compatibility for shared FE parsers
        "required_gas_celo": required_fuse,
        "gas_ready": balance_wei >= required_wei,
    }


def _has_recent_refill(checksum_wallet: str) -> tuple:
    now = time.time()
    with _faucet_lock:
        last = _faucet_recent_refill.get(checksum_wallet.lower(), 0)
    if now - last < FAUCET_DUPLICATE_WINDOW_MIN * 60:
        remaining = int((FAUCET_DUPLICATE_WINDOW_MIN * 60) - (now - last))
        return True, remaining
    return False, 0


def _record_recent_refill(checksum_wallet: str, reason: str = "unknown", source: str = "unknown", tx_hash: str = None):
    with _faucet_lock:
        _faucet_recent_refill[checksum_wallet.lower()] = time.time()
    logger.info(
        f"🧾 Faucet cooldown recorded wallet={checksum_wallet.lower()} source={source} "
        f"reason={reason} tx={tx_hash or 'n/a'}"
    )


def _check_force_onchain_rate_limit(checksum_wallet: str) -> tuple:
    """Check if wallet has exceeded force_onchain attempts per hour. Returns (is_limited, attempts_remaining, retry_after_seconds)."""
    now = time.time()
    wallet_key = checksum_wallet.lower()
    
    with _faucet_lock:
        # Clean old attempts outside the window
        attempts = _force_onchain_attempts.get(wallet_key, [])
        attempts = [ts for ts in attempts if now - ts < FAUCET_FORCE_ONCHAIN_HOUR_WINDOW]
        _force_onchain_attempts[wallet_key] = attempts
        
        # Check if limit exceeded
        if len(attempts) >= FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR:
            oldest_attempt = min(attempts)
            retry_after = int((oldest_attempt + FAUCET_FORCE_ONCHAIN_HOUR_WINDOW) - now)
            return True, 0, max(1, retry_after)
        
        return False, FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR - len(attempts), 0


def _record_force_onchain_attempt(checksum_wallet: str):
    """Record a force_onchain attempt for rate limiting."""
    wallet_key = checksum_wallet.lower()
    with _faucet_lock:
        if wallet_key not in _force_onchain_attempts:
            _force_onchain_attempts[wallet_key] = []
        _force_onchain_attempts[wallet_key].append(time.time())
    logger.warning(
        f"⚠️ Force_onchain attempt recorded wallet={wallet_key}"
    )


def _set_api_pending(checksum_wallet: str, api_tx_hash: str, pre_balance_wei: int):
    with _faucet_lock:
        _faucet_api_pending[checksum_wallet.lower()] = {
            "started_at": time.time(),
            "api_tx_hash": api_tx_hash,
            "pre_balance_wei": int(pre_balance_wei),
        }


def _get_api_pending(checksum_wallet: str):
    now = time.time()
    key = checksum_wallet.lower()
    with _faucet_lock:
        pending = _faucet_api_pending.get(key)
        if not pending:
            return None
        age = now - float(pending.get("started_at", now))
        if age > FAUCET_PENDING_TTL_SECONDS:
            _faucet_api_pending.pop(key, None)
            return None
        return {**pending, "age_seconds": int(age)}


def _clear_api_pending(checksum_wallet: str):
    with _faucet_lock:
        _faucet_api_pending.pop(checksum_wallet.lower(), None)


def _decimal_to_token_units(amount: Decimal, decimals: int) -> int:
    return int((amount * (Decimal(10) ** decimals)).to_integral_value(rounding=ROUND_CEILING))


def _token_units_to_decimal(raw: int, decimals: int) -> Decimal:
    return Decimal(int(raw)) / (Decimal(10) ** decimals)


def _get_minipay_stablecoin_balances(w3, checksum_wallet: str) -> dict:
    """Read MiniPay fee-currency balances used to decide if cUSD faucet is needed."""
    tokens = {
        "cusd": (MINIPAY_CUSD_CONTRACT, 18),
        "usdt": (MINIPAY_USDT_CONTRACT, 6),
        "usdc": (MINIPAY_USDC_CONTRACT, 6),
    }
    balances = {}
    total_usd = Decimal("0")
    for symbol, (token_addr, decimals) in tokens.items():
        try:
            contract = w3.eth.contract(
                address=Web3.to_checksum_address(token_addr),
                abi=_MINIPAY_ERC20_ABI,
            )
            raw = int(contract.functions.balanceOf(checksum_wallet).call())
        except Exception as exc:
            logger.warning(
                f"⚠️ MiniPay stablecoin balance read failed wallet={checksum_wallet.lower()} "
                f"token={symbol} error={exc}"
            )
            raw = 0
        amount = _token_units_to_decimal(raw, decimals)
        balances[symbol] = {
            "raw": str(raw),
            "balance": float(amount),
            "decimals": decimals,
            "contract": token_addr,
        }
        total_usd += amount
    return {
        "balances": balances,
        "total_usd": float(total_usd),
        "total_usd_exact": str(total_usd),
        "stable_ready": total_usd >= MINIPAY_STABLECOIN_MIN_USD,
        "required_usd": float(MINIPAY_STABLECOIN_MIN_USD),
    }


def _normalise_minipay_refill_entry(row: dict) -> dict:
    """Convert a DB row/memory entry into the cooldown payload used by the API."""
    if not row:
        return {}

    timestamp = row.get("timestamp")
    if timestamp is None:
        timestamp = _parse_iso_datetime(row.get("last_refill_at"))
        timestamp = timestamp.timestamp() if timestamp else 0

    return {
        "timestamp": float(timestamp or 0),
        "last_refill_at": row.get("last_refill_at"),
        "tx_hash": row.get("tx_hash"),
        "amount_cusd": str(row.get("amount_cusd")) if row.get("amount_cusd") is not None else None,
        "source": row.get("source") or "memory",
    }


def _get_minipay_cusd_refill_from_db(checksum_wallet: str) -> dict:
    """Fetch the latest MiniPay cUSD faucet cooldown from Supabase, if available."""
    sb = get_supabase_admin_client() or get_supabase_client()
    if not sb:
        return {}

    try:
        result = safe_supabase_operation(
            lambda: sb.table("minipay_cusd_faucet_refills")
            .select("wallet_address,last_refill_at,tx_hash,amount_cusd,updated_at")
            .eq("wallet_address", checksum_wallet.lower())
            .limit(1)
            .execute(),
            fallback_result=None,
            operation_name="get_minipay_cusd_faucet_refill",
        )
        rows = getattr(result, "data", None) or []
        if not rows:
            return {}
        return _normalise_minipay_refill_entry({**rows[0], "source": "database"})
    except Exception as exc:
        logger.warning(
            f"⚠️ MiniPay cUSD faucet DB cooldown read failed wallet={checksum_wallet.lower()}: {exc}"
        )
        return {}


def _upsert_minipay_cusd_refill_to_db(checksum_wallet: str, tx_hash: str, amount_cusd: Decimal, refill_at: datetime):
    """Persist cooldown state so restarts/multiple workers cannot bypass the 48h limit."""
    sb = get_supabase_admin_client() or get_supabase_client()
    if not sb:
        return None

    payload = {
        "wallet_address": checksum_wallet.lower(),
        "last_refill_at": refill_at.isoformat(),
        "tx_hash": tx_hash,
        "amount_cusd": str(amount_cusd),
        "updated_at": refill_at.isoformat(),
    }
    try:
        return safe_supabase_operation(
            lambda: sb.table("minipay_cusd_faucet_refills")
            .upsert(payload, on_conflict="wallet_address")
            .execute(),
            fallback_result=None,
            operation_name="upsert_minipay_cusd_faucet_refill",
        )
    except Exception as exc:
        logger.warning(
            f"⚠️ MiniPay cUSD faucet DB cooldown write failed wallet={checksum_wallet.lower()}: {exc}"
        )
        return None


def _has_recent_minipay_cusd_refill(checksum_wallet: str) -> tuple:
    now = time.time()
    wallet_key = checksum_wallet.lower()
    with _faucet_lock:
        memory_entry = _normalise_minipay_refill_entry(
            _minipay_cusd_recent_refill.get(wallet_key) or {}
        )

    db_entry = _get_minipay_cusd_refill_from_db(checksum_wallet)
    entry = max(
        [memory_entry, db_entry],
        key=lambda item: float((item or {}).get("timestamp", 0) or 0),
        default={},
    )
    last = float((entry or {}).get("timestamp", 0) or 0)
    if now - last < MINIPAY_CUSD_FAUCET_COOLDOWN_SECONDS:
        remaining = int(MINIPAY_CUSD_FAUCET_COOLDOWN_SECONDS - (now - last))
        return True, max(1, remaining), entry
    return False, 0, None


def _record_minipay_cusd_refill(checksum_wallet: str, tx_hash: str, amount_cusd: Decimal):
    refill_at = datetime.now(timezone.utc)
    entry = {
        "timestamp": refill_at.timestamp(),
        "last_refill_at": refill_at.isoformat(),
        "tx_hash": tx_hash,
        "amount_cusd": str(amount_cusd),
        "source": "memory",
    }
    with _faucet_lock:
        _minipay_cusd_recent_refill[checksum_wallet.lower()] = entry

    _upsert_minipay_cusd_refill_to_db(checksum_wallet, tx_hash, amount_cusd, refill_at)
    logger.info(
        f"🧾 MiniPay cUSD faucet cooldown recorded wallet={checksum_wallet.lower()} "
        f"amount={amount_cusd} tx={tx_hash or 'n/a'}"
    )


# ── GoodDollar Celo gas faucet 48h cooldown (persistent) ─────────────────
# Mirrors the MiniPay cUSD pattern. The in-memory `_faucet_recent_refill`
# above stays in place as a short-window dedup (~30 min). The DB-backed
# table `celo_gas_faucet_refills` enforces the longer GoodDollar coverage
# window across restarts and workers, blocking both the API path and the
# TOPWALLET_KEY on-chain fallback.
def _normalise_celo_gas_refill_entry(row: dict) -> dict:
    if not row:
        return {}

    timestamp = row.get("timestamp")
    if timestamp is None:
        parsed = _parse_iso_datetime(row.get("last_refill_at"))
        timestamp = parsed.timestamp() if parsed else 0

    return {
        "timestamp": float(timestamp or 0),
        "last_refill_at": row.get("last_refill_at"),
        "tx_hash": row.get("tx_hash"),
        "source": row.get("source") or "unknown",
    }


def _get_celo_gas_refill_from_db(checksum_wallet: str) -> dict:
    """Fetch the latest GoodDollar Celo gas refill cooldown row from Supabase, if available."""
    sb = get_supabase_admin_client() or get_supabase_client()
    if not sb:
        return {}

    try:
        result = safe_supabase_operation(
            lambda: sb.table("celo_gas_faucet_refills")
            .select("wallet_address,last_refill_at,tx_hash,source,updated_at")
            .eq("wallet_address", checksum_wallet.lower())
            .limit(1)
            .execute(),
            fallback_result=None,
            operation_name="get_celo_gas_faucet_refill",
        )
        rows = getattr(result, "data", None) or []
        if not rows:
            return {}
        return _normalise_celo_gas_refill_entry({**rows[0]})
    except Exception as exc:
        logger.warning(
            f"⚠️ GoodDollar Celo faucet DB cooldown read failed wallet={checksum_wallet.lower()}: {exc}"
        )
        return {}


def _upsert_celo_gas_refill_to_db(checksum_wallet: str, tx_hash: str, source: str, refill_at: datetime):
    """Persist the GoodDollar Celo refill timestamp so the 48h cooldown survives restarts."""
    sb = get_supabase_admin_client() or get_supabase_client()
    if not sb:
        return None

    payload = {
        "wallet_address": checksum_wallet.lower(),
        "last_refill_at": refill_at.isoformat(),
        "tx_hash": tx_hash,
        "source": source or "unknown",
        "updated_at": refill_at.isoformat(),
    }
    try:
        return safe_supabase_operation(
            lambda: sb.table("celo_gas_faucet_refills")
            .upsert(payload, on_conflict="wallet_address")
            .execute(),
            fallback_result=None,
            operation_name="upsert_celo_gas_faucet_refill",
        )
    except Exception as exc:
        logger.warning(
            f"⚠️ GoodDollar Celo faucet DB cooldown write failed wallet={checksum_wallet.lower()}: {exc}"
        )
        return None


def _has_recent_gooddollar_celo_refill(checksum_wallet: str) -> tuple:
    """Return (is_blocked, seconds_remaining, entry) using the persistent 48h cooldown."""
    db_entry = _get_celo_gas_refill_from_db(checksum_wallet)
    last = float((db_entry or {}).get("timestamp", 0) or 0)
    if not last:
        return False, 0, None
    now = time.time()
    if now - last < CELO_GAS_FAUCET_GOODDOLLAR_COOLDOWN_SECONDS:
        remaining = int(CELO_GAS_FAUCET_GOODDOLLAR_COOLDOWN_SECONDS - (now - last))
        return True, max(1, remaining), db_entry
    return False, 0, db_entry


def _record_gooddollar_celo_refill(checksum_wallet: str, tx_hash: str, source: str):
    """Persist a successful GoodDollar Celo refill (api or onchain) for the 48h cooldown."""
    refill_at = datetime.now(timezone.utc)
    _upsert_celo_gas_refill_to_db(checksum_wallet, tx_hash, source, refill_at)
    logger.info(
        f"🧾 GoodDollar Celo faucet 48h cooldown recorded wallet={checksum_wallet.lower()} "
        f"source={source} tx={tx_hash or 'n/a'} cooldown_seconds={CELO_GAS_FAUCET_GOODDOLLAR_COOLDOWN_SECONDS}"
    )


def _build_gooddollar_cooldown_payload(entry: dict, seconds_remaining: int) -> dict:
    """Common cooldown fields surfaced to the frontend banner."""
    safe_entry = entry or {}
    return {
        "gooddollar_last_refill_at": safe_entry.get("last_refill_at"),
        "gooddollar_last_refill_source": safe_entry.get("source"),
        "gooddollar_last_refill_tx_hash": safe_entry.get("tx_hash"),
        "gooddollar_cooldown_remaining_seconds": int(seconds_remaining or 0),
        "gooddollar_cooldown_total_seconds": int(CELO_GAS_FAUCET_GOODDOLLAR_COOLDOWN_SECONDS),
        "gas_coverage_message": CELO_GAS_FAUCET_COVERAGE_MESSAGE,
    }


def _execute_minipay_cusd_faucet_transfer(w3, checksum_wallet: str, amount_cusd: Decimal, correlation_id: str = "n/a") -> dict:
    """Send cUSD directly from TOPWALLET_KEY to a MiniPay wallet."""
    from blockchain import CELO_CHAIN_ID
    from eth_account import Account

    topwallet_key = (os.getenv("TOPWALLET_KEY") or "").strip()
    if not topwallet_key:
        return {
            "success": False,
            "status": "cusd_faucet_failed",
            "reason": "not_configured",
            "error": "MiniPay cUSD faucet not configured (missing TOPWALLET_KEY)",
        }

    key = topwallet_key if topwallet_key.startswith("0x") else "0x" + topwallet_key
    signer = Account.from_key(key)
    signer_masked = _mask_wallet(signer.address)
    cusd_contract = w3.eth.contract(
        address=Web3.to_checksum_address(MINIPAY_CUSD_CONTRACT),
        abi=_MINIPAY_ERC20_ABI,
    )
    amount_raw = _decimal_to_token_units(amount_cusd, 18)

    try:
        signer_cusd_raw = int(cusd_contract.functions.balanceOf(signer.address).call())
    except Exception as exc:
        return {
            "success": False,
            "status": "cusd_faucet_failed",
            "reason": "signer_balance_check_failed",
            "error": str(exc),
        }
    if signer_cusd_raw < amount_raw:
        return {
            "success": False,
            "status": "cusd_faucet_failed",
            "reason": "signer_insufficient_cusd",
            "error": "MiniPay cUSD faucet signer has insufficient cUSD",
            "signer_cusd_raw": str(signer_cusd_raw),
            "required_cusd_raw": str(amount_raw),
        }

    nonce = w3.eth.get_transaction_count(signer.address, "pending")
    tx_builder = cusd_contract.functions.transfer(checksum_wallet, amount_raw)
    try:
        gas_est = tx_builder.estimate_gas({"from": signer.address})
    except Exception:
        gas_est = 120000
    gas_price = int(w3.eth.gas_price * 1.2)
    tx = tx_builder.build_transaction({
        "chainId": CELO_CHAIN_ID,
        "from": signer.address,
        "nonce": nonce,
        "gasPrice": gas_price,
        "gas": int(gas_est * 1.2),
        "value": 0,
    })

    signer_balance_wei = int(w3.eth.get_balance(signer.address))
    estimated_tx_cost_wei = int(tx["gas"] * tx["gasPrice"])
    if signer_balance_wei < estimated_tx_cost_wei:
        return {
            "success": False,
            "status": "cusd_faucet_failed",
            "reason": "signer_insufficient_gas",
            "error": "MiniPay cUSD faucet signer has insufficient CELO for transfer gas",
            "signer_balance_wei": str(signer_balance_wei),
            "estimated_tx_cost_wei": str(estimated_tx_cost_wei),
            "shortfall_wei": str(estimated_tx_cost_wei - signer_balance_wei),
        }

    logger.info(
        f"🖊️ MiniPay cUSD faucet signer wallet={checksum_wallet.lower()} signer={signer_masked} "
        f"amount={amount_cusd} correlation_id={correlation_id}"
    )
    try:
        signed = signer.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = tx_hash.hex()
        if not tx_hash_hex.startswith("0x"):
            tx_hash_hex = "0x" + tx_hash_hex
        receipt = w3.eth.wait_for_transaction_receipt(
            tx_hash, timeout=MINIPAY_CUSD_FAUCET_RECEIPT_TIMEOUT
        )
    except Exception as exc:
        err = str(exc)
        reason = "rpc_error"
        if "insufficient funds for gas" in err.lower():
            reason = "signer_insufficient_gas"
        return {
            "success": False,
            "status": "cusd_faucet_failed",
            "reason": reason,
            "error": err,
            "signer_balance_wei": str(signer_balance_wei),
            "estimated_tx_cost_wei": str(estimated_tx_cost_wei),
        }

    if receipt and receipt.get("status") == 1:
        _record_minipay_cusd_refill(checksum_wallet, tx_hash_hex, amount_cusd)
        return {
            "success": True,
            "status": "cusd_sent",
            "tx_hash": tx_hash_hex,
            "amount_cusd": float(amount_cusd),
            "amount_cusd_raw": str(amount_raw),
        }

    return {
        "success": False,
        "status": "cusd_faucet_failed",
        "reason": "tx_failed",
        "error": "MiniPay cUSD faucet transaction failed",
        "tx_hash": tx_hash_hex,
    }


def _poll_balance_increase(w3, checksum_wallet: str, pre_balance_wei: int, wait_seconds: int, interval_seconds: int = 5):
    """Poll wallet balance for a bounded grace period."""
    checks = max(1, int(wait_seconds / max(1, interval_seconds)))
    for _ in range(checks):
        time.sleep(interval_seconds)
        post_wei = int(w3.eth.get_balance(checksum_wallet))
        if post_wei > pre_balance_wei:
            return post_wei, True
    post_wei = int(w3.eth.get_balance(checksum_wallet))
    return post_wei, post_wei > pre_balance_wei

def _mask_wallet(addr: str) -> str:
    if not addr or len(addr) < 10:
        return addr or "n/a"
    return f"{addr[:6]}...{addr[-4:]}"


def _new_faucet_flow_result(checksum_wallet: str, gas_status: dict, correlation_id: str) -> dict:
    return {
        "wallet": checksum_wallet.lower(),
        "correlation_id": correlation_id,
        "attempted_api": False,
        "attempted_onchain": False,
        "api_result": None,
        "onchain_result": None,
        "topup_source": None,
        "gas_ready": bool(gas_status.get("gas_ready")),
        "terminal_status": "gas_ready" if gas_status.get("gas_ready") else "needs_topup",
    }


def _get_faucet_correlation_id(data: dict) -> str:
    supplied = (request.headers.get("X-Correlation-ID") or data.get("correlation_id") or "").strip()
    return supplied or f"faucet-{uuid.uuid4().hex[:12]}"


def _execute_onchain_faucet_topup(w3, checksum_wallet: str, correlation_id: str = "n/a") -> dict:
    """Internal helper to send topWallet(address) tx with TOPWALLET_KEY."""
    from blockchain import CELO_CHAIN_ID
    from eth_account import Account

    games_key = (os.getenv("TOPWALLET_KEY") or "").strip()
    if not games_key:
        logger.error(
            f"❌ Faucet onchain unavailable wallet={checksum_wallet.lower()} source=onchain "
            f"reason=missing_games_key correlation_id={correlation_id}"
        )
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": "not_configured",
            "error": "On-chain faucet not configured (missing TOPWALLET_KEY)"
        }

    key = games_key if games_key.startswith("0x") else "0x" + games_key
    faucet_acct = Account.from_key(key)
    faucet_contract = Web3.to_checksum_address(GOODDOLLAR_FAUCET_CONTRACT)
    signer_masked = _mask_wallet(faucet_acct.address)
    logger.info(
        f"🖊️ Faucet onchain signer wallet={checksum_wallet.lower()} signer={signer_masked} "
        f"source=topwallet_key correlation_id={correlation_id}"
    )

    # calldata for topWallet(address): 0x3771dcf8 + padded wallet bytes
    call_data = "0x3771dcf8" + "000000000000000000000000" + checksum_wallet[2:].lower()
    nonce = w3.eth.get_transaction_count(faucet_acct.address, "pending")

    try:
        gas_est = w3.eth.estimate_gas({
            "from": faucet_acct.address,
            "to": faucet_contract,
            "data": call_data,
        })
    except Exception:
        gas_est = 140000

    tx = {
        "chainId": CELO_CHAIN_ID,
        "nonce": nonce,
        "gasPrice": int(w3.eth.gas_price * 1.2),
        "gas": int(gas_est * 1.2),
        "to": faucet_contract,
        "value": 0,
        "data": call_data,
    }

    # Preflight signer CELO balance to avoid opaque RPC failures.
    signer_balance_wei = int(w3.eth.get_balance(faucet_acct.address))
    estimated_tx_cost_wei = int(tx["gas"] * tx["gasPrice"] + tx.get("value", 0))
    if signer_balance_wei < estimated_tx_cost_wei:
        logger.error(
            f"❌ Faucet onchain insufficient signer funds wallet={checksum_wallet.lower()} source=onchain "
            f"signer={signer_masked} signer_balance_wei={signer_balance_wei} tx_cost_wei={estimated_tx_cost_wei} "
            f"shortfall_wei={estimated_tx_cost_wei - signer_balance_wei} correlation_id={correlation_id}"
        )
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": "signer_insufficient_funds",
            "error": "On-chain faucet signer has insufficient CELO for gas",
            "signer_balance_wei": str(signer_balance_wei),
            "estimated_tx_cost_wei": str(estimated_tx_cost_wei),
            "shortfall_wei": str(estimated_tx_cost_wei - signer_balance_wei),
        }

    try:
        signed = faucet_acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        tx_hash_hex = "0x" + tx_hash.hex()
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    except Exception as e:
        err = str(e)
        reason = "rpc_error"
        if "insufficient funds for gas" in err.lower():
            reason = "signer_insufficient_funds"
        logger.error(
            f"❌ Faucet onchain exception wallet={checksum_wallet.lower()} source=onchain reason={reason} "
            f"error={err} correlation_id={correlation_id}"
        )
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": reason,
            "error": err,
            "signer_balance_wei": str(signer_balance_wei),
            "estimated_tx_cost_wei": str(estimated_tx_cost_wei),
        }

    if receipt and receipt.get("status") == 1:
        logger.info(
            f"✅ Faucet onchain success wallet={checksum_wallet.lower()} source=onchain tx={tx_hash_hex} "
            f"correlation_id={correlation_id}"
        )
        _record_recent_refill(
            checksum_wallet,
            reason="onchain_tx_success",
            source="onchain",
            tx_hash=tx_hash_hex
        )
        _record_gooddollar_celo_refill(checksum_wallet, tx_hash_hex, source="onchain")
        return {"success": True, "status": "onchain_sent", "tx_hash": tx_hash_hex}

    logger.error(
        f"❌ Faucet onchain failed wallet={checksum_wallet.lower()} source=onchain tx={tx_hash_hex} "
        f"correlation_id={correlation_id}"
    )
    return {
        "success": False,
        "status": "onchain_failed",
        "error": "On-chain faucet transaction failed",
        "tx_hash": tx_hash_hex
    }


def _execute_onchain_xdc_faucet_topup(w3, checksum_wallet: str, correlation_id: str = "n/a") -> dict:
    """Internal helper to send topWallet(address) on XDC with TOPWALLET_KEY."""
    from blockchain import XDC_CHAIN_ID
    from eth_account import Account

    games_key = (os.getenv("TOPWALLET_KEY") or "").strip()
    if not games_key:
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": "not_configured",
            "error": "On-chain XDC faucet not configured (missing TOPWALLET_KEY)",
        }

    key = games_key if games_key.startswith("0x") else "0x" + games_key
    faucet_acct = Account.from_key(key)
    faucet_contract = Web3.to_checksum_address(GOODDOLLAR_XDC_FAUCET_CONTRACT)

    # calldata for topWallet(address): 0x3771dcf8 + padded wallet bytes
    call_data = "0x3771dcf8" + "000000000000000000000000" + checksum_wallet[2:].lower()
    nonce = w3.eth.get_transaction_count(faucet_acct.address, "pending")

    try:
        gas_est = w3.eth.estimate_gas({
            "from": faucet_acct.address,
            "to": faucet_contract,
            "data": call_data,
        })
    except Exception:
        gas_est = 160000

    tx = {
        "chainId": XDC_CHAIN_ID,
        "nonce": nonce,
        "gasPrice": int(w3.eth.gas_price * 12 // 10),
        "gas": int(gas_est * 12 // 10),
        "to": faucet_contract,
        "value": 0,
        "data": call_data,
    }

    try:
        signed = faucet_acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        tx_hash_hex = "0x" + tx_hash.hex()
    except Exception as e:
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": "rpc_error",
            "error": str(e),
        }

    if receipt and receipt.get("status") == 1:
        _record_recent_refill(
            checksum_wallet,
            reason="onchain_xdc_tx_success",
            source="onchain_xdc",
            tx_hash=tx_hash_hex,
        )
        return {"success": True, "status": "onchain_sent", "tx_hash": tx_hash_hex}

    return {
        "success": False,
        "status": "onchain_failed",
        "error": "On-chain XDC faucet transaction failed",
        "tx_hash": tx_hash_hex,
    }



def _execute_onchain_fuse_faucet_topup(w3, checksum_wallet: str, correlation_id: str = "n/a") -> dict:
    """Internal helper to send topWallet(address) on Fuse with TOPWALLET_KEY."""
    from blockchain import FUSE_CHAIN_ID
    from eth_account import Account

    games_key = (os.getenv("TOPWALLET_KEY") or "").strip()
    if not games_key:
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": "not_configured",
            "error": "On-chain Fuse faucet not configured (missing TOPWALLET_KEY)",
        }

    key = games_key if games_key.startswith("0x") else "0x" + games_key
    faucet_acct = Account.from_key(key)
    faucet_contract = Web3.to_checksum_address(GOODDOLLAR_FUSE_FAUCET_CONTRACT)

    # calldata for topWallet(address): 0x3771dcf8 + padded wallet bytes
    call_data = "0x3771dcf8" + "000000000000000000000000" + checksum_wallet[2:].lower()
    nonce = w3.eth.get_transaction_count(faucet_acct.address, "pending")

    try:
        gas_est = w3.eth.estimate_gas({
            "from": faucet_acct.address,
            "to": faucet_contract,
            "data": call_data,
        })
    except Exception:
        gas_est = 160000

    tx = {
        "chainId": FUSE_CHAIN_ID,
        "nonce": nonce,
        "gasPrice": int(w3.eth.gas_price * 12 // 10),
        "gas": int(gas_est * 12 // 10),
        "to": faucet_contract,
        "value": 0,
        "data": call_data,
    }

    signer_balance_wei = int(w3.eth.get_balance(faucet_acct.address))
    estimated_tx_cost_wei = int(tx["gas"] * tx["gasPrice"] + tx.get("value", 0))
    if signer_balance_wei < estimated_tx_cost_wei:
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": "signer_insufficient_funds",
            "error": "On-chain Fuse faucet signer has insufficient FUSE for gas",
            "signer_balance_wei": str(signer_balance_wei),
            "estimated_tx_cost_wei": str(estimated_tx_cost_wei),
            "shortfall_wei": str(estimated_tx_cost_wei - signer_balance_wei),
        }

    try:
        signed = faucet_acct.sign_transaction(tx)
        tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        tx_hash_hex = "0x" + tx_hash.hex()
    except Exception as e:
        err = str(e)
        reason = "signer_insufficient_funds" if "insufficient funds for gas" in err.lower() else "rpc_error"
        return {
            "success": False,
            "status": "onchain_failed",
            "reason": reason,
            "error": err,
            "signer_balance_wei": str(signer_balance_wei),
            "estimated_tx_cost_wei": str(estimated_tx_cost_wei),
        }

    if receipt and receipt.get("status") == 1:
        _record_recent_refill(
            checksum_wallet,
            reason="onchain_fuse_tx_success",
            source="onchain_fuse",
            tx_hash=tx_hash_hex,
        )
        return {"success": True, "status": "onchain_sent", "tx_hash": tx_hash_hex}

    return {
        "success": False,
        "status": "onchain_failed",
        "error": "On-chain Fuse faucet transaction failed",
        "tx_hash": tx_hash_hex,
    }


@routes.route("/api/faucet/status", methods=["POST"])
@auth_required
def faucet_status():
    """Step A for safe-claim flow: gas readiness + duplicate refill status."""
    try:
        data = request.get_json(silent=True) or {}
        correlation_id = _get_faucet_correlation_id(data)
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import CELO_RPC
        w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 15}))
        gas_status = _get_gas_status(w3, checksum_wallet)
        recent_refill, seconds_remaining = _has_recent_refill(checksum_wallet)
        gd_blocked, gd_remaining_seconds, gd_entry = _has_recent_gooddollar_celo_refill(checksum_wallet)
        pending_api = _get_api_pending(checksum_wallet)
        if gas_status.get("gas_ready"):
            status = "gas_ready"
        elif gd_blocked:
            status = "gooddollar_cooldown"
        elif recent_refill:
            status = "recent_refill"
        elif pending_api:
            status = "api_accepted_pending"
        else:
            status = "api_failed"
        logger.info(
            f"⛽ Faucet status wallet={checksum_wallet.lower()} status={status} "
            f"balance_wei={gas_status.get('balance_wei')} required_wei={gas_status.get('required_gas_wei')} "
            f"correlation_id={correlation_id}"
        )

        gooddollar_payload = _build_gooddollar_cooldown_payload(gd_entry, gd_remaining_seconds) if gd_entry else {}
        return jsonify({
            "success": True,
            "status": status,
            "correlation_id": correlation_id,
            "wallet": checksum_wallet.lower(),
            "is_recent_refill": recent_refill,
            "recent_refill_cooldown_seconds": seconds_remaining,
            "is_gooddollar_cooldown": gd_blocked,
            "pending_api": pending_api,
            "debug": {
                "required_gas_wei": gas_status.get("required_gas_wei"),
                "required_gas_celo": gas_status.get("required_gas_celo"),
                "current_balance_wei": gas_status.get("balance_wei"),
                "current_balance_celo": gas_status.get("balance_celo"),
            },
            **gooddollar_payload,
            **gas_status,
        })
    except Exception as e:
        logger.error(f"faucet_status error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/faucet/onchain", methods=["POST"])
@auth_required
def faucet_onchain():
    """Step C fallback: sign/send topWallet(address) using TOPWALLET_KEY."""
    try:
        data = request.get_json(silent=True) or {}
        correlation_id = _get_faucet_correlation_id(data)
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import CELO_RPC
        w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 15}))

        gd_blocked, gd_remaining_seconds, gd_entry = _has_recent_gooddollar_celo_refill(checksum_wallet)
        if gd_blocked:
            cooldown_hours = max(1, int(round(gd_remaining_seconds / 3600)))
            reason_msg = (
                f"GoodDollar already provided gas to this wallet. Please wait "
                f"~{cooldown_hours}h before requesting more gas."
            )
            logger.warning(
                f"🛡️ GoodDollar 48h cooldown blocking /api/faucet/onchain wallet={checksum_wallet.lower()} "
                f"remaining_seconds={gd_remaining_seconds} correlation_id={correlation_id}"
            )
            return jsonify({
                "success": False,
                "status": "gooddollar_cooldown",
                "reason": reason_msg,
                "error": reason_msg,
                "attempted_onchain": False,
                "correlation_id": correlation_id,
                **_build_gooddollar_cooldown_payload(gd_entry, gd_remaining_seconds),
            }), 429

        onchain_result = _execute_onchain_faucet_topup(
            w3, checksum_wallet, correlation_id=correlation_id
        )
        status_code = 200 if onchain_result.get("success") else 502
        if onchain_result.get("reason") == "not_configured":
            status_code = 503
        gooddollar_payload = {}
        show_msg = False
        if onchain_result.get("success"):
            _, gd_remaining_seconds_post, gd_entry_post = _has_recent_gooddollar_celo_refill(checksum_wallet)
            gooddollar_payload = _build_gooddollar_cooldown_payload(gd_entry_post, gd_remaining_seconds_post)
            show_msg = True
        return jsonify({
            **onchain_result,
            "attempted_onchain": True,
            "correlation_id": correlation_id,
            "show_gas_coverage_message": show_msg,
            **gooddollar_payload,
        }), status_code
    except Exception as e:
        err = str(e)
        logger.error(f"faucet_onchain error: {err}")
        return jsonify({"success": False, "status": "error", "error": err}), 500


@routes.route("/api/faucet/gas", methods=["POST"])
@auth_required
def faucet_gas():
    """CELO gas top-up flow: API faucet first, then TOPWALLET_KEY on-chain fallback."""
    try:
        data = request.get_json(silent=True) or {}
        correlation_id = _get_faucet_correlation_id(data)
        force_onchain = _coerce_bool(data.get("force_onchain"))
        diagnostics = {
            "correlation_id": correlation_id,
            "force_onchain": force_onchain,
            "duplicate_window_minutes": FAUCET_DUPLICATE_WINDOW_MIN,
            "stage": "init",
        }
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import CELO_RPC
        w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 15}))
        status_before = _get_gas_status(w3, checksum_wallet)
        pre_balance_wei = int(status_before["balance_wei"])
        flow_result = _new_faucet_flow_result(checksum_wallet, status_before, correlation_id)
        logger.info(
            f"⛽ Faucet gas request wallet={checksum_wallet.lower()} source=api+fallback "
            f"pre_balance_wei={pre_balance_wei} required_wei={status_before['required_gas_wei']} "
            f"force_onchain={force_onchain} correlation_id={correlation_id}"
        )
        if status_before["gas_ready"]:
            flow_result["terminal_status"] = "gas_ready"
            return jsonify({
                "success": True,
                "topped_up": False,
                "status": "gas_ready",
                **flow_result,
                "debug": {
                    "pre_balance_wei": str(pre_balance_wei),
                    "post_balance_wei": str(pre_balance_wei),
                    "required_gas_wei": status_before["required_gas_wei"],
                    "required_gas_celo": status_before["required_gas_celo"],
                },
                **status_before,
            })

        # GoodDollar 48h cooldown: blocks both API and force_onchain after a
        # successful refill. Persisted to celo_gas_faucet_refills so the
        # window survives restarts and works across multiple workers.
        gd_blocked, gd_remaining_seconds, gd_entry = _has_recent_gooddollar_celo_refill(checksum_wallet)
        if gd_blocked:
            flow_result["terminal_status"] = "gooddollar_cooldown"
            diagnostics.update({
                "stage": "blocked_gooddollar_cooldown",
                "gooddollar_cooldown_remaining_seconds": int(gd_remaining_seconds),
                "gooddollar_cooldown_total_seconds": int(CELO_GAS_FAUCET_GOODDOLLAR_COOLDOWN_SECONDS),
            })
            cooldown_hours = max(1, int(round(gd_remaining_seconds / 3600)))
            reason_msg = (
                f"GoodDollar already provided gas to this wallet. Please wait "
                f"~{cooldown_hours}h before requesting more gas — the previous 0.3 CELO "
                f"refill is intended to cover roughly 3 days of claims."
            )
            logger.warning(
                f"🛡️ GoodDollar 48h cooldown active wallet={checksum_wallet.lower()} "
                f"force_onchain={force_onchain} remaining_seconds={gd_remaining_seconds} "
                f"correlation_id={correlation_id}"
            )
            return jsonify({
                "success": False,
                "topped_up": False,
                "status": "gooddollar_cooldown",
                "reason": reason_msg,
                "error": reason_msg,
                "diagnostics": diagnostics,
                **flow_result,
                **_build_gooddollar_cooldown_payload(gd_entry, gd_remaining_seconds),
                "debug": {
                    "pre_balance_wei": str(pre_balance_wei),
                    "post_balance_wei": str(pre_balance_wei),
                    "required_gas_wei": status_before["required_gas_wei"],
                    "required_gas_celo": status_before["required_gas_celo"],
                    "cooldown_reason": "gooddollar_cooldown",
                    "force_onchain_blocked": force_onchain,
                },
                **status_before,
            }), 429

        recent_refill, seconds_remaining = _has_recent_refill(checksum_wallet)
        diagnostics.update({
            "recent_refill": bool(recent_refill),
            "recent_refill_cooldown_seconds": int(seconds_remaining),
        })
        if recent_refill:
            flow_result["terminal_status"] = "recent_refill"
            diagnostics["stage"] = "blocked_recent_refill"
            if force_onchain:
                logger.error(
                    f"❌ Faucet cooldown breach attempt wallet={checksum_wallet.lower()} source=force_onchain "
                    f"cooldown_remaining={seconds_remaining}s correlation_id={correlation_id}"
                )
            return jsonify({
                "success": True,
                "topped_up": False,
                "status": "recent_refill",
                "reason": f"Recent refill detected. Retry after ~{seconds_remaining}s.",
                "recent_refill_cooldown_seconds": seconds_remaining,
                "diagnostics": diagnostics,
                **flow_result,
                "debug": {
                    "pre_balance_wei": str(pre_balance_wei),
                    "post_balance_wei": str(pre_balance_wei),
                    "required_gas_wei": status_before["required_gas_wei"],
                    "required_gas_celo": status_before["required_gas_celo"],
                    "cooldown_reason": "recent_refill",
                    "force_onchain_blocked": force_onchain,
                },
                **status_before,
            })
        
        # Check force_onchain rate limiting
        if force_onchain:
            is_limited, attempts_remaining, retry_after = _check_force_onchain_rate_limit(checksum_wallet)
            if is_limited:
                logger.error(
                    f"❌ Faucet force_onchain rate limit exceeded wallet={checksum_wallet.lower()} "
                    f"retry_after={retry_after}s max_per_hour={FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR} "
                    f"correlation_id={correlation_id}"
                )
                return jsonify({
                    "success": False,
                    "status": "force_onchain_rate_limited",
                    "reason": f"force_onchain rate limit exceeded. Retry after ~{retry_after}s.",
                    "force_onchain_rate_limit_retry_after_seconds": retry_after,
                    "force_onchain_max_per_hour": FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR,
                    "correlation_id": correlation_id,
                    "diagnostics": {**diagnostics, "stage": "force_onchain_rate_limited"},
                }), 429
            _record_force_onchain_attempt(checksum_wallet)
            diagnostics.update({
                "force_onchain_attempts_remaining": attempts_remaining,
                "force_onchain_rate_limit_max": FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR,
            })

        # Step B: GoodDollar API faucet (skipped if force_onchain).
        api_ok = False
        api_tx_hash = None
        api_error = None
        onchain_fallback_reason = None
        if not force_onchain:
            flow_result["attempted_api"] = True
            diagnostics["stage"] = "api_faucet"
            try:
                payload = json.dumps({"chainId": 42220, "account": checksum_wallet}).encode("utf-8")
                req = urllib.request.Request(
                    GOODDOLLAR_FAUCET_API_URL,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST"
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                api_ok = body.get("ok", -1) == 1
                api_tx_hash = body.get("txHash") or body.get("tx_hash")
                api_error = None if api_ok else (body.get("error") or "API faucet declined")
            except Exception as e:
                api_error = str(e)

        flow_result["api_result"] = {
            "success": bool(api_ok),
            "tx_hash": api_tx_hash,
            "error": api_error,
        }
        diagnostics.update({
            "api_ok": bool(api_ok),
            "api_tx_hash": api_tx_hash,
            "api_error": api_error,
        })

        topup_source = None
        post_balance_wei = pre_balance_wei
        if api_ok:
            _set_api_pending(checksum_wallet, api_tx_hash, pre_balance_wei)
            post_balance_wei, increased = _poll_balance_increase(
                w3, checksum_wallet, pre_balance_wei, FAUCET_API_GRACE_SECONDS
            )
            status_after_api = _get_gas_status(w3, checksum_wallet)
            if status_after_api["gas_ready"] or increased:
                _clear_api_pending(checksum_wallet)
                _record_recent_refill(
                    checksum_wallet,
                    reason="api_balance_increase_confirmed",
                    source="api",
                    tx_hash=api_tx_hash
                )
                _record_gooddollar_celo_refill(checksum_wallet, api_tx_hash, source="api")
                _, gd_remaining_seconds, gd_entry = _has_recent_gooddollar_celo_refill(checksum_wallet)
                gooddollar_payload = _build_gooddollar_cooldown_payload(gd_entry, gd_remaining_seconds)
                return jsonify({
                    "success": True,
                    "gas_ready": status_after_api["gas_ready"],
                    "topped_up": True,
                    "topup_source": "api",
                    "api_tx_hash": api_tx_hash,
                    "api_error": api_error,
                    "onchain_result": None,
                    "status": "gas_ready" if status_after_api["gas_ready"] else "api_accepted_pending",
                    "reason": None,
                    "error": None,
                    "attempted_api": True,
                    "attempted_onchain": False,
                    "api_result": flow_result["api_result"],
                    "terminal_status": "gas_ready" if status_after_api["gas_ready"] else "api_accepted_pending",
                    "correlation_id": correlation_id,
                    "wallet": checksum_wallet.lower(),
                    "show_gas_coverage_message": True,
                    **gooddollar_payload,
                    "diagnostics": {**diagnostics, "stage": "api_success"},
                    "debug": {
                        "pre_balance_wei": str(pre_balance_wei),
                        "post_balance_wei": str(post_balance_wei),
                        "required_gas_wei": status_after_api["required_gas_wei"],
                        "required_gas_celo": status_after_api["required_gas_celo"],
                        "required_gas_reserve_wei": status_after_api["required_gas_wei"],
                        "required_gas_reserve_celo": status_after_api["required_gas_celo"],
                        "force_onchain": force_onchain,
                    },
                    **status_after_api,
                })
            onchain_fallback_reason = "api_ok_missing_txhash_or_no_balance_increase"
            logger.warning(
                f"⚠️ Faucet API pending unresolved wallet={checksum_wallet.lower()} source=api tx={api_tx_hash or 'n/a'} "
                f"post_balance_wei={post_balance_wei} fallback=onchain reason={onchain_fallback_reason} "
                f"correlation_id={correlation_id}"
            )
        elif not force_onchain:
            onchain_fallback_reason = "api_failed"

        # Step C: on-chain fallback using TOPWALLET_KEY.
        flow_result["attempted_onchain"] = True
        onchain_attempts = FAUCET_ONCHAIN_MAX_ATTEMPTS if force_onchain else 1
        onchain_attempt_history = []
        onchain_result = {}
        diagnostics["stage"] = "onchain_fallback"
        for attempt in range(onchain_attempts):
            onchain_result = _execute_onchain_faucet_topup(
                w3, checksum_wallet, correlation_id=correlation_id
            )
            onchain_attempt_history.append({
                "attempt": attempt + 1,
                "success": bool((onchain_result or {}).get("success")),
                "status": (onchain_result or {}).get("status"),
                "reason": (onchain_result or {}).get("reason"),
                "tx_hash": (onchain_result or {}).get("tx_hash"),
            })
            if onchain_result.get("success"):
                break
            if (onchain_result or {}).get("reason") == "signer_insufficient_funds":
                # Retrying without replenishing signer CELO will not help.
                break
        flow_result["onchain_result"] = onchain_result
        flow_result["onchain_attempt_history"] = onchain_attempt_history
        flow_result["onchain_attempts"] = len(onchain_attempt_history)
        if onchain_result.get("success"):
            topup_source = "onchain"
            flow_result["topup_source"] = topup_source
            _clear_api_pending(checksum_wallet)
            diagnostics["stage"] = "onchain_success"
        else:
            if api_ok:
                # Keep pending marker visible for status polling/troubleshooting.
                _set_api_pending(checksum_wallet, api_tx_hash, pre_balance_wei)
            diagnostics["stage"] = "onchain_failed"

        status_after = _get_gas_status(w3, checksum_wallet)
        post_balance_wei = int(status_after["balance_wei"])
        topped_up = bool(topup_source)
        logger.info(
            f"⛽ Faucet gas result wallet={checksum_wallet.lower()} source={topup_source or 'none'} "
            f"api_tx={api_tx_hash or 'n/a'} onchain_tx={(onchain_result or {}).get('tx_hash', 'n/a')} "
            f"pre_balance_wei={pre_balance_wei} post_balance_wei={post_balance_wei} "
            f"required_wei={status_after['required_gas_wei']} "
            f"fallback_reason={onchain_fallback_reason or 'none'} correlation_id={correlation_id}"
        )
        terminal_status = (
            "gas_ready" if status_after["gas_ready"] else
            ("onchain_sent" if topped_up else (
                "not_configured" if (onchain_result or {}).get("reason") == "not_configured" else "onchain_failed"
            ))
        )
        failure_message = (
            None if (status_after["gas_ready"] or topped_up)
            else ((onchain_result or {}).get("error") or api_error or "Gas top-up failed")
        )

        gooddollar_payload = {}
        if topped_up:
            _, gd_remaining_seconds, gd_entry = _has_recent_gooddollar_celo_refill(checksum_wallet)
            gooddollar_payload = _build_gooddollar_cooldown_payload(gd_entry, gd_remaining_seconds)

        return jsonify({
            "success": bool(status_after["gas_ready"] or topped_up),
            "wallet": checksum_wallet.lower(),
            "gas_ready": status_after["gas_ready"],
            "topped_up": topped_up,
            "topup_source": topup_source,
            "api_tx_hash": api_tx_hash,
            "api_error": api_error,
            "onchain_result": onchain_result,
            "status": terminal_status,
            "reason": failure_message,
            "error": failure_message,
            "show_gas_coverage_message": bool(topped_up),
            **gooddollar_payload,
            "diagnostics": {
                **diagnostics,
                "fallback_reason": onchain_fallback_reason,
                "onchain_success": bool((onchain_result or {}).get("success")),
                "onchain_reason": (onchain_result or {}).get("reason"),
                "onchain_tx_hash": (onchain_result or {}).get("tx_hash"),
                "onchain_attempts": len(onchain_attempt_history),
            },
            "attempted_api": flow_result["attempted_api"],
            "attempted_onchain": flow_result["attempted_onchain"],
            "api_result": flow_result["api_result"],
            "onchain_attempts": flow_result.get("onchain_attempts", 0),
            "onchain_attempt_history": flow_result.get("onchain_attempt_history", []),
            "terminal_status": terminal_status,
            "correlation_id": correlation_id,
            "debug": {
                "pre_balance_wei": str(pre_balance_wei),
                "post_balance_wei": str(post_balance_wei),
                "required_gas_wei": status_after["required_gas_wei"],
                "required_gas_celo": status_after["required_gas_celo"],
                "required_gas_reserve_wei": status_after["required_gas_wei"],
                "required_gas_reserve_celo": status_after["required_gas_celo"],
                "fallback_reason": onchain_fallback_reason,
                "force_onchain": force_onchain,
            },
            **status_after,
        })
    except Exception as e:
        logger.error(f"faucet_gas error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500



@routes.route("/api/minipay/stablecoin-faucet", methods=["POST"])
@auth_required
def minipay_stablecoin_faucet():
    """Send a tiny cUSD gas budget to MiniPay users before CELO -> cUSD swap UX."""
    try:
        data = request.get_json(silent=True) or {}
        correlation_id = _get_faucet_correlation_id(data)
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import CELO_RPC
        w3 = Web3(Web3.HTTPProvider(CELO_RPC, request_kwargs={"timeout": 15}))
        stable_before = _get_minipay_stablecoin_balances(w3, checksum_wallet)
        if stable_before["stable_ready"]:
            return jsonify({
                "success": True,
                "status": "stable_ready",
                "wallet": checksum_wallet.lower(),
                "topped_up": False,
                "stable_ready": True,
                "faucet_amount_cusd": float(MINIPAY_CUSD_FAUCET_AMOUNT),
                "program_by": MINIPAY_CUSD_FAUCET_PROGRAM_LABEL,
                "correlation_id": correlation_id,
                "stablecoin_status": stable_before,
            })

        recent_refill, seconds_remaining, recent_entry = _has_recent_minipay_cusd_refill(checksum_wallet)
        if recent_refill:
            return jsonify({
                "success": True,
                "status": "recent_refill",
                "wallet": checksum_wallet.lower(),
                "topped_up": False,
                "stable_ready": False,
                "reason": f"Recent MiniPay cUSD faucet refill detected. Retry after ~{seconds_remaining}s.",
                "recent_refill_cooldown_seconds": seconds_remaining,
                "recent_refill": recent_entry,
                "faucet_amount_cusd": float(MINIPAY_CUSD_FAUCET_AMOUNT),
                "program_by": MINIPAY_CUSD_FAUCET_PROGRAM_LABEL,
                "correlation_id": correlation_id,
                "stablecoin_status": stable_before,
            })

        transfer_result = _execute_minipay_cusd_faucet_transfer(
            w3, checksum_wallet, MINIPAY_CUSD_FAUCET_AMOUNT, correlation_id=correlation_id
        )
        status_code = 200 if transfer_result.get("success") else 502
        if transfer_result.get("reason") == "not_configured":
            status_code = 503
        stable_after = stable_before
        if transfer_result.get("success"):
            try:
                stable_after = _get_minipay_stablecoin_balances(w3, checksum_wallet)
            except Exception:
                stable_after = stable_before
        return jsonify({
            "success": bool(transfer_result.get("success")),
            "status": transfer_result.get("status") or "cusd_faucet_failed",
            "wallet": checksum_wallet.lower(),
            "topped_up": bool(transfer_result.get("success")),
            "stable_ready": bool(stable_after.get("stable_ready")),
            "topup_source": "topwallet_key_cusd" if transfer_result.get("success") else None,
            "faucet_amount_cusd": float(MINIPAY_CUSD_FAUCET_AMOUNT),
            "program_by": MINIPAY_CUSD_FAUCET_PROGRAM_LABEL,
            "correlation_id": correlation_id,
            "transfer_result": transfer_result,
            "tx_hash": transfer_result.get("tx_hash"),
            "reason": transfer_result.get("reason"),
            "error": transfer_result.get("error"),
            "stablecoin_status_before": stable_before,
            "stablecoin_status": stable_after,
        }), status_code
    except Exception as exc:
        logger.error(f"minipay_stablecoin_faucet error: {exc}")
        return jsonify({"success": False, "status": "error", "error": str(exc)}), 500



# Backward-compat endpoint used by existing clients.
@routes.route("/api/gas-faucet", methods=["POST"])
@auth_required
def gas_faucet_compat():
    return faucet_gas()


@routes.route("/api/xdc/faucet/gas", methods=["POST"])
@auth_required
def xdc_faucet_gas():
    """XDC claim-safe flow: gas readiness check + faucet top-up attempts."""
    try:
        data = request.get_json(silent=True) or {}
        correlation_id = _get_faucet_correlation_id(data)
        force_onchain = _coerce_bool(data.get("force_onchain"))
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import XDC_RPC
        w3 = Web3(Web3.HTTPProvider(XDC_RPC, request_kwargs={"timeout": 15}))

        status_before = _get_xdc_gas_status(w3, checksum_wallet)
        pre_balance_wei = int(status_before["balance_wei"])
        if status_before["gas_ready"]:
            return jsonify({
                "success": True,
                "status": "gas_ready",
                "gas_ready": True,
                "topped_up": False,
                "topup_source": None,
                "correlation_id": correlation_id,
                "wallet": checksum_wallet.lower(),
                "terminal_status": "gas_ready",
                **status_before,
            })

        recent_refill, seconds_remaining = _has_recent_refill(checksum_wallet)
        if recent_refill:
            if force_onchain:
                logger.error(
                    f"❌ Faucet cooldown breach attempt wallet={checksum_wallet.lower()} source=force_onchain "
                    f"network=xdc cooldown_remaining={seconds_remaining}s correlation_id={correlation_id}"
                )
            return jsonify({
                "success": True,
                "status": "recent_refill",
                "gas_ready": False,
                "topped_up": False,
                "terminal_status": "recent_refill",
                "recent_refill_cooldown_seconds": seconds_remaining,
                "correlation_id": correlation_id,
                "wallet": checksum_wallet.lower(),
                "debug": {
                    "pre_balance_wei": str(pre_balance_wei),
                    "post_balance_wei": str(pre_balance_wei),
                    "required_gas_wei": status_before["required_gas_wei"],
                    "required_gas_xdc": status_before["required_gas_xdc"],
                    "cooldown_reason": "recent_refill",
                    "force_onchain_blocked": force_onchain,
                },
                **status_before,
            })

        # Check force_onchain rate limiting (mirrors Celo path /api/faucet/gas).
        # Without this, repeated force_onchain=true requests after cooldown
        # expiry could still drain the TOPWALLET_KEY XDC balance.
        if force_onchain:
            is_limited, attempts_remaining, retry_after = _check_force_onchain_rate_limit(checksum_wallet)
            if is_limited:
                logger.error(
                    f"❌ Faucet force_onchain rate limit exceeded wallet={checksum_wallet.lower()} "
                    f"network=xdc retry_after={retry_after}s "
                    f"max_per_hour={FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR} correlation_id={correlation_id}"
                )
                return jsonify({
                    "success": False,
                    "status": "force_onchain_rate_limited",
                    "reason": f"force_onchain rate limit exceeded. Retry after ~{retry_after}s.",
                    "force_onchain_rate_limit_retry_after_seconds": retry_after,
                    "force_onchain_max_per_hour": FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR,
                    "correlation_id": correlation_id,
                }), 429
            _record_force_onchain_attempt(checksum_wallet)

        api_ok = False
        api_tx_hash = None
        api_error = None
        if not force_onchain:
            try:
                payload = json.dumps({"chainId": 50, "account": checksum_wallet}).encode("utf-8")
                req = urllib.request.Request(
                    GOODDOLLAR_XDC_FAUCET_API_URL,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                api_ok = body.get("ok", -1) == 1
                api_tx_hash = body.get("txHash") or body.get("tx_hash")
                api_error = None if api_ok else (body.get("error") or "API faucet declined")
            except Exception as e:
                api_error = str(e)

        # Mirror of Celo's API-first success path: accept either
        # status_after_api["gas_ready"] OR balance_increased so we don't
        # false-fail when the API ack'd but the wallet's RPC view of the
        # balance is still lagging.
        if api_ok and not force_onchain:
            _set_api_pending(checksum_wallet, api_tx_hash, pre_balance_wei)
            post_balance_wei, increased = _poll_balance_increase(
                w3, checksum_wallet, pre_balance_wei, FAUCET_API_GRACE_SECONDS
            )
            status_after_api = _get_xdc_gas_status(w3, checksum_wallet)
            if status_after_api["gas_ready"] or increased:
                _clear_api_pending(checksum_wallet)
                _record_recent_refill(
                    checksum_wallet,
                    reason="api_xdc_balance_increase_confirmed",
                    source="api_xdc",
                    tx_hash=api_tx_hash,
                )
                return jsonify({
                    "success": True,
                    "status": "gas_ready" if status_after_api["gas_ready"] else "api_accepted_pending",
                    "gas_ready": status_after_api["gas_ready"],
                    "topped_up": True,
                    "topup_source": "api",
                    "api_tx_hash": api_tx_hash,
                    "api_error": api_error,
                    "terminal_status": "gas_ready" if status_after_api["gas_ready"] else "api_accepted_pending",
                    "correlation_id": correlation_id,
                    "wallet": checksum_wallet.lower(),
                    "debug": {
                        "pre_balance_wei": str(pre_balance_wei),
                        "post_balance_wei": str(post_balance_wei),
                        "required_gas_wei": status_after_api["required_gas_wei"],
                        "required_gas_xdc": status_after_api["required_gas_xdc"],
                    },
                    **status_after_api,
                })

        # Mirror of Celo on-chain fallback: retry up to FAUCET_ONCHAIN_MAX_ATTEMPTS
        # times when force_onchain is set, otherwise just one shot. Aborts early
        # on signer_insufficient_funds since retrying won't help.
        onchain_attempts = FAUCET_ONCHAIN_MAX_ATTEMPTS if force_onchain else 1
        onchain_attempt_history = []
        onchain_result = {}
        for attempt in range(onchain_attempts):
            onchain_result = _execute_onchain_xdc_faucet_topup(
                w3, checksum_wallet, correlation_id=correlation_id
            )
            onchain_attempt_history.append({
                "attempt": attempt + 1,
                "success": bool((onchain_result or {}).get("success")),
                "status": (onchain_result or {}).get("status"),
                "reason": (onchain_result or {}).get("reason"),
                "tx_hash": (onchain_result or {}).get("tx_hash"),
            })
            if onchain_result.get("success"):
                break
            if (onchain_result or {}).get("reason") == "signer_insufficient_funds":
                break

        status_after = _get_xdc_gas_status(w3, checksum_wallet)
        topped_up = bool(onchain_result.get("success"))

        terminal_status = (
            "gas_ready" if status_after["gas_ready"] else
            ("onchain_sent" if topped_up else (
                "not_configured" if (onchain_result or {}).get("reason") == "not_configured" else "onchain_failed"
            ))
        )

        return jsonify({
            "success": bool(status_after["gas_ready"] or topped_up),
            "status": "gas_ready" if status_after["gas_ready"] else (
                "onchain_sent" if topped_up else "onchain_failed"
            ),
            "gas_ready": status_after["gas_ready"],
            "topped_up": topped_up,
            "topup_source": "onchain" if topped_up else None,
            "api_tx_hash": api_tx_hash,
            "api_error": api_error,
            "onchain_result": onchain_result,
            "onchain_attempts": len(onchain_attempt_history),
            "onchain_attempt_history": onchain_attempt_history,
            "terminal_status": terminal_status,
            "recent_refill_cooldown_seconds": seconds_remaining if recent_refill else 0,
            "correlation_id": correlation_id,
            "wallet": checksum_wallet.lower(),
            **status_after,
        })
    except Exception as e:
        logger.error(f"xdc_faucet_gas error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500


@routes.route("/api/fuse/faucet/gas", methods=["POST"])
@auth_required
def fuse_faucet_gas():
    """Fuse claim-safe flow: gas readiness check + faucet top-up attempts."""
    try:
        data = request.get_json(silent=True) or {}
        correlation_id = _get_faucet_correlation_id(data)
        force_onchain = _coerce_bool(data.get("force_onchain"))
        checksum_wallet, err_resp, status_code = _validate_and_authorize_wallet(data)
        if err_resp:
            return err_resp, status_code

        from blockchain import FUSE_RPC
        w3 = Web3(Web3.HTTPProvider(FUSE_RPC, request_kwargs={"timeout": 15}))

        status_before = _get_fuse_gas_status(w3, checksum_wallet)
        pre_balance_wei = int(status_before["balance_wei"])
        if status_before["gas_ready"]:
            return jsonify({
                "success": True,
                "status": "gas_ready",
                "gas_ready": True,
                "topped_up": False,
                "topup_source": None,
                "correlation_id": correlation_id,
                "wallet": checksum_wallet.lower(),
                "terminal_status": "gas_ready",
                **status_before,
            })

        recent_refill, seconds_remaining = _has_recent_refill(checksum_wallet)
        if recent_refill:
            if force_onchain:
                logger.error(
                    f"❌ Faucet cooldown breach attempt wallet={checksum_wallet.lower()} source=force_onchain "
                    f"network=fuse cooldown_remaining={seconds_remaining}s correlation_id={correlation_id}"
                )
            return jsonify({
                "success": True,
                "status": "recent_refill",
                "gas_ready": False,
                "topped_up": False,
                "terminal_status": "recent_refill",
                "recent_refill_cooldown_seconds": seconds_remaining,
                "correlation_id": correlation_id,
                "wallet": checksum_wallet.lower(),
                "debug": {
                    "pre_balance_wei": str(pre_balance_wei),
                    "post_balance_wei": str(pre_balance_wei),
                    "required_gas_wei": status_before["required_gas_wei"],
                    "required_gas_fuse": status_before["required_gas_fuse"],
                    "cooldown_reason": "recent_refill",
                    "force_onchain_blocked": force_onchain,
                },
                **status_before,
            })

        if force_onchain:
            is_limited, attempts_remaining, retry_after = _check_force_onchain_rate_limit(checksum_wallet)
            if is_limited:
                logger.error(
                    f"❌ Faucet force_onchain rate limit exceeded wallet={checksum_wallet.lower()} "
                    f"network=fuse retry_after={retry_after}s "
                    f"max_per_hour={FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR} correlation_id={correlation_id}"
                )
                return jsonify({
                    "success": False,
                    "status": "force_onchain_rate_limited",
                    "reason": f"force_onchain rate limit exceeded. Retry after ~{retry_after}s.",
                    "force_onchain_rate_limit_retry_after_seconds": retry_after,
                    "force_onchain_max_per_hour": FAUCET_FORCE_ONCHAIN_MAX_PER_HOUR,
                    "correlation_id": correlation_id,
                }), 429
            _record_force_onchain_attempt(checksum_wallet)

        api_ok = False
        api_tx_hash = None
        api_error = None
        if not force_onchain:
            try:
                payload = json.dumps({"chainId": 122, "account": checksum_wallet}).encode("utf-8")
                req = urllib.request.Request(
                    GOODDOLLAR_FUSE_FAUCET_API_URL,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                api_ok = body.get("ok", -1) == 1
                api_tx_hash = body.get("txHash") or body.get("tx_hash")
                api_error = None if api_ok else (body.get("error") or "API faucet declined")
            except Exception as e:
                api_error = str(e)

        if api_ok and not force_onchain:
            _set_api_pending(checksum_wallet, api_tx_hash, pre_balance_wei)
            post_balance_wei, increased = _poll_balance_increase(
                w3, checksum_wallet, pre_balance_wei, FAUCET_API_GRACE_SECONDS
            )
            status_after_api = _get_fuse_gas_status(w3, checksum_wallet)
            if status_after_api["gas_ready"] or increased:
                _clear_api_pending(checksum_wallet)
                _record_recent_refill(
                    checksum_wallet,
                    reason="api_fuse_balance_increase_confirmed",
                    source="api_fuse",
                    tx_hash=api_tx_hash,
                )
                return jsonify({
                    "success": True,
                    "status": "gas_ready" if status_after_api["gas_ready"] else "api_accepted_pending",
                    "gas_ready": status_after_api["gas_ready"],
                    "topped_up": True,
                    "topup_source": "api",
                    "api_tx_hash": api_tx_hash,
                    "api_error": api_error,
                    "terminal_status": "gas_ready" if status_after_api["gas_ready"] else "api_accepted_pending",
                    "correlation_id": correlation_id,
                    "wallet": checksum_wallet.lower(),
                    "debug": {
                        "pre_balance_wei": str(pre_balance_wei),
                        "post_balance_wei": str(post_balance_wei),
                        "required_gas_wei": status_after_api["required_gas_wei"],
                        "required_gas_fuse": status_after_api["required_gas_fuse"],
                    },
                    **status_after_api,
                })

        onchain_attempts = FAUCET_ONCHAIN_MAX_ATTEMPTS if force_onchain else 1
        onchain_attempt_history = []
        onchain_result = {}
        for attempt in range(onchain_attempts):
            onchain_result = _execute_onchain_fuse_faucet_topup(
                w3, checksum_wallet, correlation_id=correlation_id
            )
            onchain_attempt_history.append({
                "attempt": attempt + 1,
                "success": bool((onchain_result or {}).get("success")),
                "status": (onchain_result or {}).get("status"),
                "reason": (onchain_result or {}).get("reason"),
                "tx_hash": (onchain_result or {}).get("tx_hash"),
            })
            if onchain_result.get("success"):
                break
            if (onchain_result or {}).get("reason") == "signer_insufficient_funds":
                break

        status_after = _get_fuse_gas_status(w3, checksum_wallet)
        topped_up = bool(onchain_result.get("success"))

        terminal_status = (
            "gas_ready" if status_after["gas_ready"] else
            ("onchain_sent" if topped_up else (
                "not_configured" if (onchain_result or {}).get("reason") == "not_configured" else "onchain_failed"
            ))
        )

        return jsonify({
            "success": bool(status_after["gas_ready"] or topped_up),
            "status": "gas_ready" if status_after["gas_ready"] else (
                "onchain_sent" if topped_up else "onchain_failed"
            ),
            "gas_ready": status_after["gas_ready"],
            "topped_up": topped_up,
            "topup_source": "onchain" if topped_up else None,
            "api_tx_hash": api_tx_hash,
            "api_error": api_error,
            "onchain_result": onchain_result,
            "onchain_attempts": len(onchain_attempt_history),
            "onchain_attempt_history": onchain_attempt_history,
            "terminal_status": terminal_status,
            "recent_refill_cooldown_seconds": seconds_remaining if recent_refill else 0,
            "correlation_id": correlation_id,
            "wallet": checksum_wallet.lower(),
            **status_after,
        })
    except Exception as e:
        logger.error(f"fuse_faucet_gas error: {e}")
        return jsonify({"success": False, "error": str(e)}), 500
