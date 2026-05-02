"""
Audit: For users in `user_data` whose first_login is between --from and --to (UTC),
check on-chain (via Celo RPC eth_getLogs) whether the wallet received a GoodDollar
UBI Scheme Transfer on the SAME calendar date (UTC) as their first_login.

Strategy (fast, no Celoscan rate limits):
  1) Pull rows from Supabase user_data where first_login ∈ [from, to] AND wallet_address IS NOT NULL.
  2) For each wallet, compute the day-window [00:00 UTC, 24:00 UTC] of first_login.
     - Find startBlock = first block >= startOfDay (binary search on eth_getBlockByNumber)
     - Find endBlock   = last block  <= endOfDay   (binary search)
  3) eth_getLogs on the GoodDollar token contract:
        topic0 = Transfer(address,address,uint256)
        topic1 = UBI Scheme proxy (padded)        -> from
        topic2 = wallet (padded)                  -> to
        fromBlock..toBlock
     -> If any logs returned, the wallet got a UBI claim on the same date as first_login.
  4) Output CSV + summary.

Read-only: nothing is written to Supabase or onchain.

Usage:
    python scripts/match_first_login_first_claim.py --from 2026-04-23 --to 2026-05-01

Env required:
    SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY (or SUPABASE_KEY/SUPABASE_ANON_KEY)
    CELO_RPC_URL  (or RPC_URL)  -- defaults to https://forno.celo.org
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import requests
from supabase import create_client

# ---- Constants ----------------------------------------------------------------
GOODDOLLAR_TOKEN = "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"  # G$ on Celo
UBI_PROXY = "0x43d72Ff17701B2DA814620735C39C620Ce0ea4A1"          # GoodDollar UBI Scheme
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

DEFAULT_RPC = "https://forno.celo.org"


def addr_to_topic(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    return "0x" + ("0" * (64 - len(a))) + a


# ---- RPC ---------------------------------------------------------------------
class RPC:
    def __init__(self, url: str):
        self.url = url
        self.s = requests.Session()
        self._id = 0

    def call(self, method: str, params: list[Any], retries: int = 5) -> Any:
        backoff = 1.0
        for attempt in range(retries):
            self._id += 1
            try:
                r = self.s.post(
                    self.url,
                    json={"jsonrpc": "2.0", "id": self._id, "method": method, "params": params},
                    timeout=30,
                )
                if r.status_code == 429:
                    time.sleep(backoff)
                    backoff *= 2
                    continue
                r.raise_for_status()
                data = r.json()
                if "error" in data:
                    err = data["error"]
                    msg = err.get("message", "")
                    # transient errors: retry
                    if "rate" in msg.lower() or "timeout" in msg.lower() or err.get("code") == -32005:
                        time.sleep(backoff)
                        backoff *= 2
                        continue
                    raise RuntimeError(f"RPC error: {err}")
                return data["result"]
            except (requests.RequestException, ValueError) as e:
                if attempt == retries - 1:
                    raise
                time.sleep(backoff)
                backoff *= 2
        raise RuntimeError(f"RPC failed after {retries} retries")

    def block_number(self) -> int:
        return int(self.call("eth_blockNumber", []), 16)

    def block_timestamp(self, block: int) -> int:
        b = self.call("eth_getBlockByNumber", [hex(block), False])
        if not b:
            raise RuntimeError(f"block {block} not found")
        return int(b["timestamp"], 16)

    def get_logs(self, params: dict) -> list[dict]:
        return self.call("eth_getLogs", [params])


# ---- Block search ------------------------------------------------------------
def find_block_at_or_after(rpc: RPC, ts: int, lo: int, hi: int) -> int:
    """Return smallest block N in [lo,hi] with timestamp(N) >= ts."""
    if rpc.block_timestamp(lo) >= ts:
        return lo
    if rpc.block_timestamp(hi) < ts:
        return hi + 1
    while lo < hi:
        mid = (lo + hi) // 2
        if rpc.block_timestamp(mid) < ts:
            lo = mid + 1
        else:
            hi = mid
    return lo


def find_block_at_or_before(rpc: RPC, ts: int, lo: int, hi: int) -> int:
    """Return largest block N in [lo,hi] with timestamp(N) <= ts."""
    if rpc.block_timestamp(hi) <= ts:
        return hi
    if rpc.block_timestamp(lo) > ts:
        return lo - 1
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if rpc.block_timestamp(mid) > ts:
            hi = mid - 1
        else:
            lo = mid
    return lo


# ---- Helpers -----------------------------------------------------------------
def parse_iso(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def day_bounds_utc(d: datetime) -> tuple[datetime, datetime]:
    s = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=timezone.utc)
    return s, s + timedelta(days=1)


# ---- Main --------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="dfrom", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--to", dest="dto", required=True, help="YYYY-MM-DD (UTC, inclusive)")
    ap.add_argument("--out", default="audit_first_login_vs_first_claim.csv")
    args = ap.parse_args()

    sb_url = os.environ.get("SUPABASE_URL")
    sb_key = (
        os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        or os.environ.get("SUPABASE_KEY")
        or os.environ.get("SUPABASE_ANON_KEY")
    )
    if not sb_url or not sb_key:
        print("ERROR: SUPABASE_URL and a Supabase key must be set.", file=sys.stderr)
        return 2
    using_service = bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
    print(f"[supabase] using {'service-role' if using_service else 'anon'} key", flush=True)

    rpc_url = os.environ.get("CELO_RPC_URL") or os.environ.get("RPC_URL") or DEFAULT_RPC
    print(f"[rpc] {rpc_url}", flush=True)

    sb = create_client(sb_url, sb_key)
    rpc = RPC(rpc_url)

    start_iso = f"{args.dfrom}T00:00:00+00:00"
    end_iso = f"{args.dto}T23:59:59+00:00"
    print(f"[supabase] user_data first_login in [{start_iso} .. {end_iso}]", flush=True)

    res = (
        sb.table("user_data")
        .select("id,wallet_address,first_login,verified_after_goodmarket,verification_timestamp,username")
        .gte("first_login", start_iso)
        .lte("first_login", end_iso)
        .not_.is_("wallet_address", None)
        .order("first_login", desc=False)
        .execute()
    )
    rows = res.data or []
    print(f"[supabase] {len(rows)} candidate users", flush=True)
    if not rows:
        return 0

    latest_block = rpc.block_number()
    print(f"[rpc] latest block = {latest_block:,}", flush=True)

    ubi_topic = addr_to_topic(UBI_PROXY)
    out_rows: list[dict] = []
    matches = 0
    no_claim = 0
    errors = 0

    for i, r in enumerate(rows, 1):
        wallet = (r.get("wallet_address") or "").lower()
        username = r.get("username") or ""
        verified = bool(r.get("verified_after_goodmarket"))
        v_ts = parse_iso(r.get("verification_timestamp"))
        login_dt = parse_iso(r.get("first_login"))
        if not wallet or not login_dt:
            continue

        day_start, day_end = day_bounds_utc(login_dt)
        ts_start = int(day_start.timestamp())
        ts_end = int(day_end.timestamp()) - 1

        print(
            f"[{i:2}/{len(rows)}] {wallet} ({username or '-'}) "
            f"first_login={login_dt.isoformat()}",
            flush=True,
        )

        try:
            start_block = find_block_at_or_after(rpc, ts_start, 1, latest_block)
            end_block = find_block_at_or_before(rpc, ts_end, 1, latest_block)
            if start_block > end_block:
                print("    no blocks in day window", flush=True)
                out_rows.append({
                    "wallet_address": wallet,
                    "username": username,
                    "first_login_utc": login_dt.isoformat(),
                    "first_login_date": login_dt.strftime("%Y-%m-%d"),
                    "verified_after_goodmarket": verified,
                    "verification_timestamp_utc": v_ts.isoformat() if v_ts else "",
                    "claim_found_on_first_login_date": False,
                    "first_claim_tx": "",
                    "first_claim_block": "",
                    "first_claim_utc": "",
                    "match": "NO_CLAIM_ON_DATE",
                })
                no_claim += 1
                continue

            logs = rpc.get_logs({
                "fromBlock": hex(start_block),
                "toBlock": hex(end_block),
                "address": GOODDOLLAR_TOKEN,
                "topics": [TRANSFER_TOPIC, ubi_topic, addr_to_topic(wallet)],
            })

            if not logs:
                print(f"    no UBI claim on {login_dt.strftime('%Y-%m-%d')}", flush=True)
                out_rows.append({
                    "wallet_address": wallet,
                    "username": username,
                    "first_login_utc": login_dt.isoformat(),
                    "first_login_date": login_dt.strftime("%Y-%m-%d"),
                    "verified_after_goodmarket": verified,
                    "verification_timestamp_utc": v_ts.isoformat() if v_ts else "",
                    "claim_found_on_first_login_date": False,
                    "first_claim_tx": "",
                    "first_claim_block": "",
                    "first_claim_utc": "",
                    "match": "NO_CLAIM_ON_DATE",
                })
                no_claim += 1
                continue

            # logs are typically ordered; pick the earliest
            logs.sort(key=lambda lg: (int(lg["blockNumber"], 16), int(lg.get("logIndex", "0x0"), 16)))
            first = logs[0]
            blk = int(first["blockNumber"], 16)
            tx = first["transactionHash"]
            blk_ts = rpc.block_timestamp(blk)
            claim_dt = datetime.fromtimestamp(blk_ts, tz=timezone.utc)
            amount_raw = int(first["data"], 16) if first.get("data") and first["data"] != "0x" else 0
            amount = amount_raw / (10**18)

            print(
                f"    CLAIM at block {blk} tx {tx} {claim_dt.isoformat()} amount~{amount:.4f} G$",
                flush=True,
            )
            out_rows.append({
                "wallet_address": wallet,
                "username": username,
                "first_login_utc": login_dt.isoformat(),
                "first_login_date": login_dt.strftime("%Y-%m-%d"),
                "verified_after_goodmarket": verified,
                "verification_timestamp_utc": v_ts.isoformat() if v_ts else "",
                "claim_found_on_first_login_date": True,
                "first_claim_tx": tx,
                "first_claim_block": blk,
                "first_claim_utc": claim_dt.isoformat(),
                "match": "MATCH_SAME_DATE",
            })
            matches += 1

        except Exception as e:
            print(f"    ERROR: {e}", flush=True)
            errors += 1
            out_rows.append({
                "wallet_address": wallet,
                "username": username,
                "first_login_utc": login_dt.isoformat(),
                "first_login_date": login_dt.strftime("%Y-%m-%d"),
                "verified_after_goodmarket": verified,
                "verification_timestamp_utc": v_ts.isoformat() if v_ts else "",
                "claim_found_on_first_login_date": "",
                "first_claim_tx": "",
                "first_claim_block": "",
                "first_claim_utc": "",
                "match": f"ERROR: {e}",
            })

    if out_rows:
        out_path = args.out
        with open(out_path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
            w.writeheader()
            w.writerows(out_rows)
        print(f"\n[output] wrote {len(out_rows)} rows -> {out_path}", flush=True)

    print("\n=== SUMMARY ===", flush=True)
    print(f"  candidates scanned : {len(rows)}", flush=True)
    print(f"  same-date matches  : {matches}", flush=True)
    print(f"  no claim on date   : {no_claim}", flush=True)
    print(f"  errors             : {errors}", flush=True)

    if matches:
        print("\n=== MATCHES (first_login date == first claim date) ===", flush=True)
        for row in out_rows:
            if row["match"] == "MATCH_SAME_DATE":
                print(
                    f"  {row['wallet_address']}  {row['username'] or '-':<20}  "
                    f"date={row['first_login_date']}  tx={row['first_claim_tx']}",
                    flush=True,
                )

    return 0


if __name__ == "__main__":
    sys.exit(main())
