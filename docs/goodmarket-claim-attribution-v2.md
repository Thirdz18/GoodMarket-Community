# GoodMarket Claim Attribution V2 (Version B)

## Scope
Track **GoodMarket-only** G$ claims with auditable, backend-verified attribution.

## SQL Schema

```sql
create table if not exists goodmarket_claim_events (
  id uuid primary key default gen_random_uuid(),
  claim_attempt_id uuid not null,
  wallet_address text not null,
  network text not null check (network in ('celo','xdc')),
  tx_hash text,
  event_type text not null check (event_type in (
    'claim_flow_started',
    'claim_tx_submitted',
    'claim_tx_confirmed',
    'claim_tx_failed',
    'claim_tx_rejected'
  )),
  source text not null default 'goodmarket_wallet_ui' check (source in ('goodmarket_wallet_ui')),
  correlation_id text,
  session_fingerprint text,
  user_agent_hash text,
  error_code text,
  error_message text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_gm_claim_events_created_at on goodmarket_claim_events(created_at desc);
create index if not exists idx_gm_claim_events_wallet_created on goodmarket_claim_events(wallet_address, created_at desc);
create index if not exists idx_gm_claim_events_network_created on goodmarket_claim_events(network, created_at desc);
create index if not exists idx_gm_claim_events_tx_hash on goodmarket_claim_events(tx_hash);
create index if not exists idx_gm_claim_events_attempt on goodmarket_claim_events(claim_attempt_id);

create unique index if not exists uq_gm_claim_events_idempotency
  on goodmarket_claim_events (claim_attempt_id, event_type, coalesce(tx_hash, ''));


create table if not exists goodmarket_claim_facts (
  id uuid primary key default gen_random_uuid(),
  wallet_address text not null,
  network text not null check (network in ('celo','xdc')),
  tx_hash text not null unique,
  submitted_at timestamptz,
  confirmed_at timestamptz,
  status text not null check (status in ('submitted','confirmed','failed','rejected','unknown')),
  source text not null default 'goodmarket_wallet_ui' check (source in ('goodmarket_wallet_ui')),
  claim_attempt_id uuid,
  correlation_id text,
  block_number bigint,
  tx_from text,
  tx_to text,
  verification_state text not null default 'pending' check (verification_state in ('pending','verified','mismatch')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_gm_claim_facts_confirmed_at on goodmarket_claim_facts(confirmed_at desc);
create index if not exists idx_gm_claim_facts_wallet_confirmed on goodmarket_claim_facts(wallet_address, confirmed_at desc);
create index if not exists idx_gm_claim_facts_network_confirmed on goodmarket_claim_facts(network, confirmed_at desc);
create index if not exists idx_gm_claim_facts_status_confirmed on goodmarket_claim_facts(status, confirmed_at desc);
```

## API Contracts

### 1) `POST /api/claims/v2/start`
Request
```json
{
  "wallet": "0x...",
  "network_intent": "celo",
  "correlation_id": "claim-..."
}
```
Response
```json
{
  "success": true,
  "claim_attempt_id": "uuid",
  "source": "goodmarket_wallet_ui"
}
```

### 2) `POST /api/claims/v2/submitted`
Request
```json
{
  "claim_attempt_id": "uuid",
  "wallet": "0x...",
  "network": "celo",
  "tx_hash": "0x...",
  "correlation_id": "claim-..."
}
```
Response
```json
{
  "success": true,
  "queued_for_verification": true
}
```

### 3) `POST /api/claims/v2/finalize` (optional frontend signal)
Request
```json
{
  "claim_attempt_id": "uuid",
  "wallet": "0x...",
  "network": "celo",
  "tx_hash": "0x...",
  "wallet_observed_status": "success"
}
```
Response
```json
{
  "success": true,
  "note": "final source of truth is async verifier"
}
```

### 4) `GET /api/claims/v2/metrics?from=...&to=...&tz=UTC`
Response
```json
{
  "success": true,
  "range": {"from": "...", "to": "...", "tz": "UTC"},
  "kpi": {
    "unique_confirmed_claimers": 123,
    "confirmed_claims": 456,
    "submitted_claims": 480
  },
  "by_network": {
    "celo": {"unique_confirmed_claimers": 100, "confirmed_claims": 300},
    "xdc": {"unique_confirmed_claimers": 40, "confirmed_claims": 156}
  }
}
```

## Async Verifier (Pseudo-code)

```text
loop every 10s:
  rows = select * from goodmarket_claim_facts
         where status='submitted' and verification_state='pending'
         order by created_at asc limit 200

  for row in rows:
    receipt = get_receipt(row.network, row.tx_hash)
    if receipt not found:
      if row older than retry window: mark status='unknown'
      continue

    tx = get_transaction(row.network, row.tx_hash)

    if tx.to != expected_claim_contract(network):
      update row set verification_state='mismatch', status='failed'
      append event claim_tx_failed (reason: contract mismatch)
      continue

    if receipt.status == 1:
      update row set status='confirmed', confirmed_at=now(), verification_state='verified',
                     block_number=receipt.blockNumber, tx_from=tx.from, tx_to=tx.to
      append event claim_tx_confirmed
    else:
      update row set status='failed', verification_state='verified', block_number=receipt.blockNumber
      append event claim_tx_failed
```

## KPI SQL Samples

### Daily unique confirmed claimers (authoritative)
```sql
select
  date_trunc('day', confirmed_at at time zone 'UTC') as day_utc,
  count(distinct wallet_address) as unique_confirmed_claimers
from goodmarket_claim_facts
where status = 'confirmed'
  and source = 'goodmarket_wallet_ui'
  and confirmed_at >= :from_ts
  and confirmed_at <  :to_ts
group by 1
order by 1;
```

### Daily totals + conversion
```sql
with submitted as (
  select date_trunc('day', submitted_at at time zone 'UTC') d, count(*) c
  from goodmarket_claim_facts
  where submitted_at >= :from_ts and submitted_at < :to_ts
  group by 1
),
confirmed as (
  select date_trunc('day', confirmed_at at time zone 'UTC') d, count(*) c
  from goodmarket_claim_facts
  where status='confirmed' and confirmed_at >= :from_ts and confirmed_at < :to_ts
  group by 1
)
select
  coalesce(s.d, c.d) as day_utc,
  coalesce(s.c, 0) as submitted_claims,
  coalesce(c.c, 0) as confirmed_claims,
  case when coalesce(s.c,0)=0 then null else round((coalesce(c.c,0)::numeric / s.c::numeric)*100,2) end as conversion_pct
from submitted s
full outer join confirmed c on s.d = c.d
order by 1;
```

## Acceptance Criteria
- One tx is counted once (dedupe by unique tx_hash in facts table).
- Only `status='confirmed'` contributes to authoritative KPI.
- Mismatch/failed/rejected excluded from confirmed KPI.
- Metrics are UTC-normalized.
- End-to-end trace is possible via `claim_attempt_id` and `correlation_id`.
