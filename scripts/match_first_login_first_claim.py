"""
Match user_data.first_login (April 23 -> May 1, 2026, UTC) with each wallet's
FIRST EVER GoodDollar UBI claim on Celo.

Approach (optimized batched scan):
  1. Pull user_data rows where first_login is in window.
  2. Resolve block range covering [HISTORY_FLOOR_BLOCK -> tip].
  3. Single chunked eth_getLogs scan over the GoodDollar token contract for:
       Transfer(from=UBI_PROXY, to=ANY_OF(our wallets))
     Using a topic[2] array means ONE filter covers all 22+ wallets in parallel.
  4. For each wallet, take the earliest matched log -> that is the first claim.
  5. Compare calendar date (UTC) of first_login vs first_claim.

CSV output: audit_first_login_vs_first_claim.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

try:
    from supabase import create_client  # type: ignore
except Exception as exc:
    print(f"[error] supabase python client not available: {exc}")
    sys.exit(1)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CELO_RPC = os.getenv("CELO_RPC_URL", "https://forno.celo.org")
CHAIN_ID = int(os.getenv("CHAIN_ID", "42220"))

UBI_PROXY = os.getenv(
    "UBI_PROXY_CONTRACT", "0x43d72Ff17701B2DA814620735C39C620Ce0ea4A1"
).lower()
GOODDOLLAR_TOKEN = os.getenv(
    "GOODDOLLAR_TOKEN_CONTRACT", "0x62B8B11039FcfE5aB0C56E502b1C372A3d2a9c7A"
).lower()

# ERC20 Transfer(address indexed from, address indexed to, uint256)
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
)

# UBIScheme proxy was deployed on Celo around block 5,200,000 (Sept 2020).
# 2,000,000 is a safe lower bound that covers all GoodDollar history.
HISTORY_FLOOR_BLOCK = int(os.getenv("HISTORY_FLOOR_BLOCK", "2000000"))

# Forno typical max is ~10k blocks per filter. 9000 is a safe chunk size.
LOG_RANGE_CHUNK = int(os.getenv("LOG_RANGE_CHUNK", "9000"))

SESSION = requests.Session()


# ---------------------------------------------------------------------------
# RPC helpers
# ---------------------------------------------------------------------------

_rpc_id = 0


def _rpc(method: str, params: List[Any]) -> Any:
    global _rpc_id
    _rpc_id += 1
    payload = {"jsonrpc": "2.0", "id": _rpc_id, "method": method, "params": params}
    last_err: Optional[Exception] = None
    for attempt in range(5):
        try:
            r = SESSION.post(CELO_RPC, json=payload, timeout=45)
            r.raise_for_status()
            data = r.json()
            if "error" in data:
                err = data["error"]
                msg = str(err.get("message", err))
                raise RuntimeError(f"rpc_error: {msg}")
            return data["result"]
        except (requests.RequestException, RuntimeError) as e:
            last_err = e
            time.sleep(1 + attempt)
    raise last_err  # type: ignore[misc]


def block_by_number(num_hex: str) -> Optional[Dict[str, Any]]:
    return _rpc("eth_getBlockByNumber", [num_hex, False])


def latest_block() -> int:
    return int(_rpc("eth_blockNumber", []), 16)


def addr_topic(addr: str) -> str:
    a = addr.lower().replace("0x", "")
    return "0x" + a.rjust(64, "0")


def topic_to_addr(topic_hex: str) -> str:
    return "0x" + topic_hex[-40:].lower()


def get_logs_chunk(from_block: int, to_block: int, topics: List[Any]) -> List[Dict]:
    params = {
        "address": GOODDOLLAR_TOKEN,
        "fromBlock": hex(from_block),
        "toBlock": hex(to_block),
        "topics": topics,
    }
    try:
        return _rpc("eth_getLogs", [params]) or []
    except RuntimeError as e:
        msg = str(e).lower()
        if any(s in msg for s in ("range", "limit", "too", "size", "many")):
            if from_block >= to_block:
                raise
            mid = (from_block + to_block) // 2
            left = get_logs_chunk(from_block, mid, topics)
            right = get_logs_chunk(mid + 1, to_block, topics)
            return left + right
        raise


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------


def supabase_client():
    url = os.getenv("SUPABASE_URL")
    key = (
        os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_SERVICE_KEY")
        or os.getenv("SUPABASE_KEY")
        or os.getenv("SUPABASE_ANON_KEY")
    )
    if not url or not key:
        print("[error] SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY missing")
        sys.exit(1)
    is_service = bool(
        os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
    )
    print(f"[info] supabase using {'SERVICE_ROLE' if is_service else 'ANON'} key")
    return create_client(url, key)


def fetch_users(sb, start_iso: str, end_iso: str) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    page = 0
    while True:
        q = (
            sb.table("user_data")
            .select(
                "id,wallet_address,first_login,verified_after_goodmarket,verification_timestamp,username"
            )
            .gte("first_login", start_iso)
            .lte("first_login", end_iso)
            .order("first_login")
            .range(page * 1000, (page + 1) * 1000 - 1)
        )
        res = q.execute()
        batch = res.data or []
        rows.extend(batch)
        if len(batch) < 1000:
            break
        page += 1
    return rows


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def parse_iso_to_dt(s: Any) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(
            timezone.utc
        )
    except Exception:
        return None


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="d_from", default="2026-04-23")
    p.add_argument("--to", dest="d_to", default="2026-05-01")
    p.add_argument(
        "--out", default="audit_first_login_vs_first_claim.csv", help="output csv path"
    )
    p.add_argument(
        "--floor",
        type=int,
        default=HISTORY_FLOOR_BLOCK,
        help="lower bound block to search",
    )
    args = p.parse_args()

    start_dt = datetime.strptime(args.d_from, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(args.d_to, "%Y-%m-%d").replace(
        hour=23, minute=59, second=59, tzinfo=timezone.utc
    )

    print(f"[info] window:        {start_dt.isoformat()} -> {end_dt.isoformat()}")
    print(f"[info] RPC:           {CELO_RPC} (chain_id={CHAIN_ID})")
    print(f"[info] UBI_PROXY:     {UBI_PROXY}")
    print(f"[info] GD_TOKEN:      {GOODDOLLAR_TOKEN}")
    print(f"[info] HISTORY_FLOOR: {args.floor}")

    # --- 1) users ---
    sb = supabase_client()
    rows = fetch_users(sb, start_dt.isoformat(), end_dt.isoformat())
    print(f"[info] user_data rows in window: {len(rows)}")
    rows = [r for r in rows if r.get("wallet_address")]
    print(f"[info] with wallet_address:      {len(rows)}")
    if not rows:
        print("[done] no users in window; nothing to do.")
        return

    # de-dup wallet list, keep first row per wallet (lowest first_login)
    by_wallet: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        w = r["wallet_address"].lower().strip()
        if w not in by_wallet:
            by_wallet[w] = r
    wallets = sorted(by_wallet.keys())
    print(f"[info] unique wallets:           {len(wallets)}")

    # --- 2) tip + scan range ---
    tip = latest_block()
    print(f"[info] latest block:             {tip}")
    from_block = args.floor
    to_block = tip
    total_blocks = to_block - from_block
    n_chunks = (total_blocks + LOG_RANGE_CHUNK - 1) // LOG_RANGE_CHUNK
    print(
        f"[info] scanning blocks {from_block} -> {to_block} "
        f"({total_blocks:,} blocks, ~{n_chunks} chunks)"
    )

    # --- 3) ONE chunked scan covering all wallets ---
    # topics: [Transfer, from=UBI_PROXY, to=ANY_OF(wallets)]
    to_topic_array = [addr_topic(w) for w in wallets]
    topics = [TRANSFER_TOPIC, addr_topic(UBI_PROXY), to_topic_array]

    earliest_log_per_wallet: Dict[str, Tuple[int, int, str]] = {}
    # value = (block_number, log_index, tx_hash)

    cur = from_block
    chunk_idx = 0
    found_total = 0
    t0 = time.time()
    while cur <= to_block:
        end = min(cur + LOG_RANGE_CHUNK - 1, to_block)
        chunk_idx += 1
        try:
            logs = get_logs_chunk(cur, end, topics)
        except Exception as e:
            print(f"[warn] chunk {chunk_idx} ({cur}-{end}) failed: {e}; skipping")
            cur = end + 1
            continue
        if logs:
            for lg in logs:
                bn = int(lg["blockNumber"], 16)
                li = int(lg["logIndex"], 16)
                tx = lg["transactionHash"]
                # topics[2] = recipient
                wallet = topic_to_addr(lg["topics"][2])
                cur_first = earliest_log_per_wallet.get(wallet)
                if cur_first is None or (bn, li) < (cur_first[0], cur_first[1]):
                    earliest_log_per_wallet[wallet] = (bn, li, tx)
            found_total += len(logs)
        if chunk_idx % 25 == 0 or cur == from_block:
            elapsed = time.time() - t0
            pct = (chunk_idx / max(n_chunks, 1)) * 100
            print(
                f"[scan] chunk {chunk_idx}/{n_chunks} "
                f"({pct:.1f}%) block={end:,} "
                f"logs_found={found_total} matched_wallets={len(earliest_log_per_wallet)} "
                f"elapsed={elapsed:.1f}s"
            )
        cur = end + 1

    print(
        f"[scan] DONE chunks={chunk_idx} logs_found={found_total} "
        f"matched_wallets={len(earliest_log_per_wallet)}"
    )

    # --- 4) resolve block timestamps for first claims ---
    print("[info] resolving block timestamps for first claims...")
    block_ts_cache: Dict[int, int] = {}
    first_claim_resolved: Dict[str, Tuple[int, int, str]] = {}
    for wallet, (bn, li, tx) in earliest_log_per_wallet.items():
        if bn not in block_ts_cache:
            try:
                blk = block_by_number(hex(bn))
                block_ts_cache[bn] = int(blk["timestamp"], 16) if blk else 0
            except Exception as e:
                print(f"[warn] block {bn} ts fetch failed: {e}")
                block_ts_cache[bn] = 0
        ts = block_ts_cache[bn]
        first_claim_resolved[wallet] = (bn, ts, tx)

    # --- 5) build report ---
    out_rows: List[Dict[str, Any]] = []
    matches: List[Dict[str, Any]] = []
    for wallet in wallets:
        r = by_wallet[wallet]
        first_login = parse_iso_to_dt(r.get("first_login"))
        verified_via_gm = r.get("verified_after_goodmarket")
        v_at = parse_iso_to_dt(r.get("verification_timestamp"))
        username = r.get("username") or ""

        info = first_claim_resolved.get(wallet)
        if info:
            bn, ts, tx = info
            first_claim_dt = (
                datetime.fromtimestamp(ts, tz=timezone.utc) if ts else None
            )
            first_claim_block = bn
            first_claim_tx = tx
        else:
            first_claim_dt = None
            first_claim_block = ""
            first_claim_tx = ""

        same_date = False
        delta_seconds: Any = ""
        if first_login and first_claim_dt:
            same_date = first_login.date() == first_claim_dt.date()
            delta_seconds = int((first_claim_dt - first_login).total_seconds())

        row = {
            "wallet_address": wallet,
            "username": username,
            "first_login_utc": first_login.isoformat() if first_login else "",
            "first_login_date_utc": first_login.date().isoformat() if first_login else "",
            "first_claim_utc": first_claim_dt.isoformat() if first_claim_dt else "",
            "first_claim_date_utc": first_claim_dt.date().isoformat() if first_claim_dt else "",
            "same_calendar_date_utc": "YES" if same_date else "NO",
            "delta_seconds_claim_minus_login": delta_seconds,
            "first_claim_tx": first_claim_tx,
            "first_claim_block": first_claim_block,
            "verified_after_goodmarket": bool(verified_via_gm),
            "verification_timestamp_utc": v_at.isoformat() if v_at else "",
            "celoscan_url": (
                f"https://celoscan.io/tx/{first_claim_tx}" if first_claim_tx else ""
            ),
        }
        out_rows.append(row)
        if same_date:
            matches.append(row)

    # --- 6) write CSV + summary ---
    out_path = args.out
    with open(out_path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(out_rows[0].keys()))
        w.writeheader()
        w.writerows(out_rows)

    print("\n" + "=" * 78)
    print(f"[done] wrote {len(out_rows)} rows -> {out_path}")
    print(f"[summary] users in window:        {len(wallets)}")
    print(f"[summary] users with any claim:   {len(first_claim_resolved)}")
    print(f"[summary] same-date matches:      {len(matches)}")
    print("=" * 78)
    if matches:
        print("\nMATCHED (first_login date == first_claim date, UTC):")
        for m in matches:
            uname = f"  ({m['username']})" if m["username"] else ""
            print(
                f"  {m['wallet_address']}{uname}\n"
                f"    login : {m['first_login_utc']}\n"
                f"    claim : {m['first_claim_utc']}\n"
                f"    tx    : {m['celoscan_url']}\n"
            )
    else:
        print("\nNo wallets had first_claim on the same UTC date as first_login.")


if __name__ == "__main__":
    main()
