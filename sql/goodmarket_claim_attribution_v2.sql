-- ─────────────────────────────────────────────────────────────────────────────
-- GoodMarket Claim Attribution V2 (Version B)
-- Tables backing /api/claims/v2/confirm and the analytics
-- "goodmarket_unique_claimers" / "goodmarket_total_claims" KPIs.
--
-- Source of truth for the schema: docs/goodmarket-claim-attribution-v2.md
--
-- Run this in the Supabase SQL Editor against the project database.
-- It is idempotent — safe to re-run any time.
-- ─────────────────────────────────────────────────────────────────────────────

-- ── Events: append-only audit trail of every step in the claim flow ─────────
create table if not exists public.goodmarket_claim_events (
    id                  uuid primary key default gen_random_uuid(),
    claim_attempt_id    uuid not null,
    wallet_address      text not null,
    network             text not null check (network in ('celo','xdc')),
    tx_hash             text,
    event_type          text not null check (event_type in (
        'claim_flow_started',
        'claim_tx_submitted',
        'claim_tx_confirmed',
        'claim_tx_failed',
        'claim_tx_rejected'
    )),
    -- Loose CHECK so future surfaces (mobile shell, MiniPay, partner widgets)
    -- can record claims without us needing a schema migration each time.
    source              text not null default 'goodmarket_wallet_ui'
                              check (length(source) between 1 and 64),
    correlation_id      text,
    session_fingerprint text,
    user_agent_hash     text,
    error_code          text,
    error_message       text,
    metadata            jsonb not null default '{}'::jsonb,
    created_at          timestamptz not null default now()
);

create index if not exists idx_gm_claim_events_created_at
    on public.goodmarket_claim_events(created_at desc);
create index if not exists idx_gm_claim_events_wallet_created
    on public.goodmarket_claim_events(wallet_address, created_at desc);
create index if not exists idx_gm_claim_events_network_created
    on public.goodmarket_claim_events(network, created_at desc);
create index if not exists idx_gm_claim_events_tx_hash
    on public.goodmarket_claim_events(tx_hash);
create index if not exists idx_gm_claim_events_attempt
    on public.goodmarket_claim_events(claim_attempt_id);

-- Idempotency: at most one (attempt, event_type, tx_hash) row.
-- coalesce(tx_hash, '') so events without tx_hash (e.g. claim_flow_started)
-- still dedupe per attempt.
create unique index if not exists uq_gm_claim_events_idempotency
    on public.goodmarket_claim_events
       (claim_attempt_id, event_type, coalesce(tx_hash, ''));


-- ── Facts: one row per unique tx_hash. Authoritative KPI source. ────────────
create table if not exists public.goodmarket_claim_facts (
    id                 uuid primary key default gen_random_uuid(),
    wallet_address     text not null,
    network            text not null check (network in ('celo','xdc')),
    tx_hash            text not null unique,
    submitted_at       timestamptz,
    confirmed_at       timestamptz,
    status             text not null check (status in (
        'submitted','confirmed','failed','rejected','unknown'
    )),
    source             text not null default 'goodmarket_wallet_ui'
                              check (length(source) between 1 and 64),
    claim_attempt_id   uuid,
    correlation_id     text,
    block_number       bigint,
    tx_from            text,
    tx_to              text,
    verification_state text not null default 'pending'
                              check (verification_state in ('pending','verified','mismatch')),
    created_at         timestamptz not null default now(),
    updated_at         timestamptz not null default now()
);

create index if not exists idx_gm_claim_facts_confirmed_at
    on public.goodmarket_claim_facts(confirmed_at desc);
create index if not exists idx_gm_claim_facts_wallet_confirmed
    on public.goodmarket_claim_facts(wallet_address, confirmed_at desc);
create index if not exists idx_gm_claim_facts_network_confirmed
    on public.goodmarket_claim_facts(network, confirmed_at desc);
create index if not exists idx_gm_claim_facts_status_confirmed
    on public.goodmarket_claim_facts(status, confirmed_at desc);


-- ── Row Level Security ──────────────────────────────────────────────────────
-- Writes are SERVER-ONLY (service-role bypasses RLS regardless of policies).
-- Reads are allowed to the anon role so the analytics dashboard
-- (`goodmarket_unique_claimers`, `goodmarket_total_claims`) keeps working
-- even when SUPABASE_SERVICE_ROLE_KEY isn't configured. The data here is
-- non-sensitive: tx_hash and wallet_address are already public on-chain.
alter table public.goodmarket_claim_events enable row level security;
alter table public.goodmarket_claim_facts  enable row level security;

-- Drop any older variants so this migration is safely re-runnable.
drop policy if exists "gm_claim_events_anon_read" on public.goodmarket_claim_events;
drop policy if exists "gm_claim_facts_anon_read"  on public.goodmarket_claim_facts;
drop policy if exists "gm_claim_events_auth_read" on public.goodmarket_claim_events;
drop policy if exists "gm_claim_facts_auth_read"  on public.goodmarket_claim_facts;

-- SELECT for anon + authenticated. NO insert/update/delete policies →
-- only the service-role key can write.
create policy "gm_claim_events_anon_read"
    on public.goodmarket_claim_events
    for select
    to anon, authenticated
    using (true);

create policy "gm_claim_facts_anon_read"
    on public.goodmarket_claim_facts
    for select
    to anon, authenticated
    using (true);


-- ── Optional convenience view for KPI queries ───────────────────────────────
create or replace view public.v_goodmarket_claim_kpi_daily as
select
    date_trunc('day', confirmed_at at time zone 'UTC') as day_utc,
    network,
    count(*) filter (where status = 'confirmed')           as confirmed_claims,
    count(distinct wallet_address)
        filter (where status = 'confirmed')                as unique_confirmed_claimers
from public.goodmarket_claim_facts
where confirmed_at is not null
group by 1, 2
order by 1 desc, 2;

comment on table public.goodmarket_claim_facts  is
    'GoodMarket claim attribution V2 — one row per tx_hash. Authoritative KPI source.';
comment on table public.goodmarket_claim_events is
    'GoodMarket claim attribution V2 — append-only event trail.';
