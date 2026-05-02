#!/usr/bin/env python3
"""
Audit: First Login vs First UBI Claim (Verified via GoodMarket)
================================================================

For every wallet in ``user_data`` whose ``first_login`` falls inside the
target window AND whose ``verified_after_goodmarket`` flag is TRUE, this
script:

  1. Reads the ``first_login`` timestamp from Supabase.
  2. Queries Celoscan for the wallet's earliest transaction to the
     GoodDollar UBI Proxy contract (``UBI_PROXY``). That tx is the
     wallet's "first claim" on-chain.
  3. Compares the UTC date of ``first_login`` to the UTC date of the
     first claim and flags whether they match (same day).

Output:
    - A console table summarising every wallet checked.
    - A JSON dump at ``/tmp/audit_first_login_vs_first_claim.json``
      with the full detail (timestamps, tx hashes, celoscan links).

Default window: 2026-04-23 → 2026-05-01 (inclusive).

Usage:
    # From the project root, with env vars sourced:
    set -a && source .env.preview && set +a
    python scripts/audit_first_login_vs_first_claim.py

    # Custom window:
    python scripts/audit_first_login_vs_first_claim.py \
        --from 2026-04-23 --to 2026-05-01
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import requests
from supabase import create_client


# ─── Config ─────────────────────────────────────────────────────────────────

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_SERVICE_ROLE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY")

# UBI Proxy contract on Celo. Same default as blockchain.py.
UBI_PROXY = os.getenv("UBI_PROXY_CONTRACT",
                      "0x43d72Ff17701B2DA814620735C39C620Ce0ea4A1").lower()

# Celoscan v2 API (works without key, but with stricter rate limits).
CELOSCAN_API = "https://api.celoscan.io/api"
CELOSCAN_API_KEY = os.getenv("CELOSCAN_API_KEY", "").strip()

# Throttle between Celoscan calls. Public (no key) tier is ~1 req/5s.
# With a key we can go ~5 req/s.
THROTTLE_S = 1.2 if CELOSCAN_API_KEY else 5.5

OUT_FILE = "/tmp/audit_first_login_vs_first_claim.json"


# ─── Helpers ────────────────────────────────────────────────────────────────

@dataclass
class WalletAudit:
    wallet_address: str
    first_login_iso: Optional[str]
    first_login_date_utc: Optional[str]
    verified_after_goodmarket: bool
    first_claim_tx: Optional[str] = None
    first_claim_iso: Optional[str] = None
    first_claim_date_utc: Optional[str] = None
    dates_match: Optional[bool] = None
    celoscan_url: Optional[str] = None
    notes: List[str] = field(default_factory=list)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--from", dest="from_date", default="2026-04-23",
                   help="Start date inclusive, YYYY-MM-DD UTC (default 2026-04-23).")
    p.add_argument("--to", dest="to_date", default="2026-05-01",
                   help="End date inclusive, YYYY-MM-DD UTC (default 2026-05-01).")
    p.add_argument("--limit", type=int, default=500,
                   help="Max wallets to audit (default 500).")
    return p.parse_args()


def _to_iso_bounds(from_date: str, to_date: str) -> tuple[str, str]:
    """Convert YYYY-MM-DD bounds (inclusive) into ISO timestamps suitable for
    a half-open ``[start, end_exclusive)`` Supabase filter."""
    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_inclusive = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_exclusive = end_inclusive + timedelta(days=1)
    return start.isoformat(), end_exclusive.isoformat()


def _utc_date(iso_or_dt: Any) -> Optional[str]:
    """Extract YYYY-MM-DD UTC from either an ISO string or a datetime."""
    if not iso_or_dt:
        return None
    if isinstance(iso_or_dt, datetime):
        dt = iso_or_dt
    else:
        s = str(iso_or_dt).replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _get_supabase():
    key = SUPABASE_SERVICE_ROLE_KEY or SUPABASE_ANON_KEY
    if not SUPABASE_URL or not key:
        sys.stderr.write(
            "ERROR: SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY (or _ANON_KEY) "
            "are not set. Source your .env file before running.\n"
        )
        sys.exit(2)
    if not SUPABASE_SERVICE_ROLE_KEY:
        sys.stderr.write(
            "WARN: using anon key. RLS may hide rows — prefer service role.\n"
        )
    return create_client(SUPABASE_URL, key)


def _fetch_target_users(sb, start_iso: str, end_iso: str, limit: int
                        ) -> List[Dict[str, Any]]:
    """user_data rows with first_login in [start, end) AND verified_after_goodmarket = TRUE."""
    resp = (
        sb.table("user_data")
          .select("wallet_address, first_login, verified_after_goodmarket, "
                  "ubi_verified, face_verified")
          .gte("first_login", start_iso)
          .lt("first_login", end_iso)
          .eq("verified_after_goodmarket", True)
          .order("first_login", desc=False)
          .limit(limit)
          .execute()
    )
    return resp.data or []


def _celoscan_first_ubi_claim(wallet: str) -> Dict[str, Any]:
    """Return ``{tx_hash, timestamp, ...}`` for the wallet's earliest tx to
    the UBI Proxy contract, or ``{"found": False, ...}``.

    Uses the ``account.txlist`` endpoint with ascending sort and filters
    in Python (the API doesn't let us filter by ``to=`` directly).
    """
    params = {
        "module": "account",
        "action": "txlist",
        "address": wallet,
        "startblock": 0,
        "endblock": 99999999,
        "page": 1,
        "offset": 1000,  # max per page; we only need the first claim
        "sort": "asc",
    }
    if CELOSCAN_API_KEY:
        params["apikey"] = CELOSCAN_API_KEY

    try:
        r = requests.get(CELOSCAN_API, params=params, timeout=20)
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        return {"found": False, "error": f"celoscan_request_failed: {exc}"}

    # Celoscan returns status="0" for both "no tx" and "rate limit". Disambiguate.
    if data.get("status") != "1":
        msg = (data.get("message") or data.get("result") or "").lower()
        if "no transactions" in msg:
            return {"found": False, "error": "no_transactions"}
        return {"found": False, "error": f"celoscan_status_0: {data.get('message')} / {data.get('result')}"}

    txs = data.get("result") or []
    for tx in txs:
        # Filter to txs sent BY the wallet TO the UBI proxy. Successful only
        # (isError=='0'). The first one in ascending order is the first claim.
        if (tx.get("to") or "").lower() != UBI_PROXY:
            continue
        if (tx.get("from") or "").lower() != wallet.lower():
            continue
        if str(tx.get("isError", "0")) == "1":
            continue
        ts = int(tx.get("timeStamp", "0"))
        if ts <= 0:
            continue
        return {
            "found": True,
            "tx_hash": tx.get("hash"),
            "timestamp": ts,
            "iso": datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(),
            "block_number": tx.get("blockNumber"),
            "method_id": (tx.get("methodId") or "")[:10],
        }

    return {"found": False, "error": "no_ubi_proxy_tx_in_first_page"}


# ─── Main ───────────────────────────────────────────────────────────────────

def main() -> int:
    args = _parse_args()
    start_iso, end_iso = _to_iso_bounds(args.from_date, args.to_date)

    print(f"[audit] window: {args.from_date} 00:00 UTC → {args.to_date} 23:59 UTC (inclusive)")
    print(f"[audit] supabase filter: first_login >= {start_iso} AND first_login < {end_iso}")
    print(f"[audit] verified_after_goodmarket = TRUE")
    print(f"[audit] UBI_PROXY = {UBI_PROXY}")
    print(f"[audit] celoscan throttle: {THROTTLE_S}s between requests "
          f"(api key {'set' if CELOSCAN_API_KEY else 'NOT set — public limit'})")
    print()

    sb = _get_supabase()
    users = _fetch_target_users(sb, start_iso, end_iso, args.limit)
    print(f"[audit] matched {len(users)} user_data rows in window\n")

    if not users:
        print("No users match the filter. Done.")
        return 0

    audits: List[WalletAudit] = []
    same_day = 0
    different_day = 0
    no_claim = 0

    for i, row in enumerate(users, 1):
        wallet = (row.get("wallet_address") or "").strip()
        first_login_iso = row.get("first_login")
        audit = WalletAudit(
            wallet_address=wallet,
            first_login_iso=first_login_iso,
            first_login_date_utc=_utc_date(first_login_iso),
            verified_after_goodmarket=bool(row.get("verified_after_goodmarket")),
            celoscan_url=f"https://celoscan.io/address/{wallet}",
        )

        if not wallet:
            audit.notes.append("missing_wallet_address")
            audits.append(audit)
            continue

        result = _celoscan_first_ubi_claim(wallet)
        if result.get("found"):
            audit.first_claim_tx = result["tx_hash"]
            audit.first_claim_iso = result["iso"]
            audit.first_claim_date_utc = _utc_date(result["iso"])
            audit.dates_match = (
                audit.first_login_date_utc is not None
                and audit.first_claim_date_utc == audit.first_login_date_utc
            )
            if audit.dates_match:
                same_day += 1
            else:
                different_day += 1
        else:
            audit.notes.append(result.get("error", "no_claim"))
            no_claim += 1

        audits.append(audit)

        flag = (
            "MATCH " if audit.dates_match
            else ("DIFF  " if audit.first_claim_iso else "NO_TX ")
        )
        print(f"  [{i:>3}/{len(users)}] {flag} {wallet}  "
              f"login={audit.first_login_date_utc}  "
              f"claim={audit.first_claim_date_utc or '—'}  "
              f"tx={(audit.first_claim_tx or '—')[:14]}…")

        # Stay under the rate limit.
        if i < len(users):
            time.sleep(THROTTLE_S)

    # ── Summary ─────────────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print(f"Total audited                    : {len(audits)}")
    print(f"  same-day (first_login==first_claim)  : {same_day}")
    print(f"  different-day                        : {different_day}")
    print(f"  no UBI claim found on Celoscan       : {no_claim}")
    print("=" * 78)

    # ── Detailed table ──────────────────────────────────────────────────────
    print()
    print(f"{'wallet':<44}  {'login_date':<10}  {'claim_date':<10}  match  first_claim_tx")
    print("-" * 120)
    for a in audits:
        match = "yes" if a.dates_match else ("no " if a.first_claim_iso else "-- ")
        print(f"{a.wallet_address:<44}  "
              f"{(a.first_login_date_utc or '—'):<10}  "
              f"{(a.first_claim_date_utc or '—'):<10}  "
              f"{match:<5}  "
              f"{a.first_claim_tx or ''}")

    # ── JSON dump ───────────────────────────────────────────────────────────
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "window": {"from": args.from_date, "to": args.to_date},
        "ubi_proxy": UBI_PROXY,
        "totals": {
            "audited": len(audits),
            "same_day": same_day,
            "different_day": different_day,
            "no_claim": no_claim,
        },
        "rows": [asdict(a) for a in audits],
    }
    with open(OUT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\n[audit] full results written to {OUT_FILE}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
