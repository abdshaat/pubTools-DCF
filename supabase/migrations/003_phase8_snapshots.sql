-- Phase 8 Slice C: durable normalized financial snapshots, current ticker
-- heads, and the daily-refresh run/claim ledger (ADR-005/006/007, ADR-008).
--
-- Run this in the Supabase SQL editor after 002_phase6_customer_login.sql and
-- BEFORE deploying application code that enables the database read-through:
-- once SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY are configured the app treats a
-- missing table as a storage error (controlled 503), not as a cache miss.
--
-- ADR-008: nothing in this schema stores a market price or quote. The
-- snapshot jsonb holds the price-free normalized statement payload plus the
-- raw provider profile used as normalization metadata; the live price comes
-- from Finnhub per request and is never persisted anywhere.
--
-- Rollback (reverse dependency order):
--   drop function public.store_ticker_snapshot(text, text, jsonb, text, integer, date, text, text);
--   drop table public.financial_refresh_claims;
--   drop table public.financial_refresh_runs;
--   drop table public.ticker_snapshot_heads;
--   drop table public.normalized_snapshots;
--
-- Backups/restore: rely on Supabase managed backups (Dashboard -> Database ->
-- Backups). Restoring these tables restores the entire statement store; Redis
-- is a rebuildable accelerator on top and needs no restore.
--
-- Retention: snapshots are a few KB per ticker-period -- keep indefinitely.
-- Deleting refresh claims/runs past their operational window is a documented
-- manual service-role operation, never automatic.
--
-- Rollback additions for the run-orchestration RPCs:
--   drop function public.begin_financial_refresh_run(date, timestamptz);
--   drop function public.finish_financial_refresh_run(date);

-- Immutable content-addressed statement documents. snapshot_version is
-- "sha256:<hex>" over the canonical JSON of the price-free normalized
-- payload (same recipe as the response's data_version), so a later provider
-- fetch that re-confirms an identical filing hashes identically and the
-- ON CONFLICT DO NOTHING in store_ticker_snapshot() keeps exactly one row.
create table if not exists public.normalized_snapshots (
    snapshot_version text primary key,
    ticker text not null,
    snapshot jsonb not null,
    provider text not null default 'financialmodelingprep',
    fiscal_year integer,
    statement_date date,
    currency text,
    created_at timestamptz not null default now()
);

create index if not exists normalized_snapshots_ticker_history_idx
    on public.normalized_snapshots(ticker, statement_date desc, created_at desc);

-- Mutable current pointer per ticker: which snapshot is live, when it was
-- last verified against the provider, and the scheduled-refresh bookkeeping.
-- This is an observation record, not historical evidence -- history lives in
-- normalized_snapshots.
create table if not exists public.ticker_snapshot_heads (
    ticker text primary key,
    snapshot_version text not null references public.normalized_snapshots(snapshot_version),
    verified_at timestamptz not null,
    last_requested_at timestamptz,
    last_refresh_attempt_at timestamptz,
    last_refresh_success_at timestamptz,
    refresh_status text,
    updated_at timestamptz not null default now()
);

-- One row per Eastern calendar date: the durable claim that makes the dual
-- UTC cron schedules idempotent, plus reconcilable run counters (ADR-007).
create table if not exists public.financial_refresh_runs (
    refresh_date date primary key,
    scheduled_window_at timestamptz,
    started_at timestamptz,
    finished_at timestamptz,
    status text,
    total_tickers integer,
    attempted_tickers integer,
    succeeded_tickers integer,
    failed_tickers integer,
    error_code text
);

-- Per-ticker manifest claims for a run: every DB ticker gets a pending claim
-- at run start and must end success or explicit failure -- no silent omission.
-- The (ticker, refresh_date) primary key is the durable once-per-day provider
-- gate; duplicate cron delivery or Redis loss cannot spend a second cycle.
create table if not exists public.financial_refresh_claims (
    ticker text not null,
    refresh_date date not null references public.financial_refresh_runs(refresh_date) on delete cascade,
    claimed_at timestamptz,
    completed_at timestamptz,
    status text,
    error_code text,
    primary key (ticker, refresh_date)
);

alter table public.normalized_snapshots enable row level security;
alter table public.ticker_snapshot_heads enable row level security;
alter table public.financial_refresh_runs enable row level security;
alter table public.financial_refresh_claims enable row level security;

-- ADR-005: historical snapshots are immutable. Revoking UPDATE/DELETE from
-- every request-facing role (including service_role) makes that a database
-- guarantee, not an application convention; store_ticker_snapshot() below
-- runs as its definer, so inserts still work while mutation cannot.
revoke update, delete on public.normalized_snapshots from public, anon, authenticated, service_role;

-- Atomic snapshot insert + head upsert (ADR-006: publication requires the
-- durable write; the app awaits this RPC before touching Redis or returning).
create or replace function public.store_ticker_snapshot(
    p_ticker text,
    p_snapshot_version text,
    p_snapshot jsonb,
    p_provider text,
    p_fiscal_year integer,
    p_statement_date date,
    p_currency text,
    p_refresh_status text
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_ticker is null or length(trim(p_ticker)) = 0 then
        raise exception 'ticker is required';
    end if;
    if p_snapshot_version is null or length(trim(p_snapshot_version)) = 0 then
        raise exception 'snapshot version is required';
    end if;
    if p_snapshot is null then
        raise exception 'snapshot payload is required';
    end if;

    insert into public.normalized_snapshots
        (snapshot_version, ticker, snapshot, provider, fiscal_year, statement_date, currency)
    values (
        p_snapshot_version,
        upper(p_ticker),
        p_snapshot,
        coalesce(nullif(trim(p_provider), ''), 'financialmodelingprep'),
        p_fiscal_year,
        p_statement_date,
        p_currency
    )
    on conflict (snapshot_version) do nothing;

    insert into public.ticker_snapshot_heads (
        ticker,
        snapshot_version,
        verified_at,
        last_refresh_attempt_at,
        last_refresh_success_at,
        refresh_status,
        updated_at
    )
    values (
        upper(p_ticker),
        p_snapshot_version,
        now(),
        now(),
        now(),
        coalesce(nullif(trim(p_refresh_status), ''), 'bootstrap_snapshot'),
        now()
    )
    on conflict (ticker) do update set
        snapshot_version = excluded.snapshot_version,
        verified_at = excluded.verified_at,
        last_refresh_attempt_at = excluded.last_refresh_attempt_at,
        last_refresh_success_at = excluded.last_refresh_success_at,
        refresh_status = excluded.refresh_status,
        updated_at = excluded.updated_at;
end;
$$;

-- Atomic daily-run claim + manifest creation (ADR-007). The refresh_date
-- primary-key insert is the durable idempotency gate for the dual UTC cron
-- schedules: exactly one invocation per Eastern date creates the run, and it
-- snapshots EVERY current database ticker into a pending claim -- no activity
-- filter, budget skip, or silent deferral. A duplicate delivery gets
-- already_claimed=true and must do nothing.
create or replace function public.begin_financial_refresh_run(
    p_refresh_date date,
    p_scheduled_window_at timestamptz
)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_total integer;
    v_tickers text[];
begin
    if p_refresh_date is null then
        raise exception 'refresh date is required';
    end if;

    insert into public.financial_refresh_runs (refresh_date, scheduled_window_at, started_at, status)
    values (p_refresh_date, p_scheduled_window_at, now(), 'running')
    on conflict (refresh_date) do nothing;

    if not found then
        return jsonb_build_object(
            'already_claimed', true,
            'status', (
                select status from public.financial_refresh_runs
                where refresh_date = p_refresh_date
            )
        );
    end if;

    insert into public.financial_refresh_claims (ticker, refresh_date, claimed_at, status)
    select ticker, p_refresh_date, now(), 'pending'
    from public.ticker_snapshot_heads;

    select coalesce(array_agg(ticker order by ticker), '{}'::text[])
    into v_tickers
    from public.financial_refresh_claims
    where refresh_date = p_refresh_date;
    v_total := coalesce(array_length(v_tickers, 1), 0);

    update public.financial_refresh_runs
    set total_tickers = v_total
    where refresh_date = p_refresh_date;

    return jsonb_build_object(
        'already_claimed', false,
        'status', 'running',
        'total_tickers', v_total,
        'tickers', to_jsonb(v_tickers)
    );
end;
$$;

-- Run completion reconciles counts FROM the claims (the durable evidence),
-- never from in-memory counters. A claim still pending at finish means an
-- unprocessed ticker: the run can only end partial_failed, never silently
-- shrink the manifest.
create or replace function public.finish_financial_refresh_run(p_refresh_date date)
returns jsonb
language plpgsql
security definer
set search_path = public
as $$
declare
    v_total integer;
    v_succeeded integer;
    v_failed integer;
    v_pending integer;
    v_status text;
begin
    select
        count(*),
        count(*) filter (where status = 'succeeded'),
        count(*) filter (where status = 'failed'),
        count(*) filter (where status not in ('succeeded', 'failed'))
    into v_total, v_succeeded, v_failed, v_pending
    from public.financial_refresh_claims
    where refresh_date = p_refresh_date;

    v_status := case
        when v_pending > 0 then 'partial_failed'
        when v_failed = 0 then 'succeeded'
        when v_succeeded = 0 and v_total > 0 then 'failed'
        else 'partial_failed'
    end;

    update public.financial_refresh_runs
    set finished_at = now(),
        status = v_status,
        attempted_tickers = v_succeeded + v_failed,
        succeeded_tickers = v_succeeded,
        failed_tickers = v_failed
    where refresh_date = p_refresh_date;

    return jsonb_build_object(
        'status', v_status,
        'total', v_total,
        'succeeded', v_succeeded,
        'failed', v_failed,
        'pending', v_pending
    );
end;
$$;

-- Same PostgREST lockdown as migrations 001/002: without this, anyone with
-- the anon/authenticated key could call the SECURITY DEFINER functions over
-- HTTP. Only the service role (used server-side by this app) may call them.
revoke execute on function public.store_ticker_snapshot(text, text, jsonb, text, integer, date, text, text) from public;
revoke execute on function public.begin_financial_refresh_run(date, timestamptz) from public;
revoke execute on function public.finish_financial_refresh_run(date) from public;
grant execute on function public.store_ticker_snapshot(text, text, jsonb, text, integer, date, text, text) to service_role;
grant execute on function public.begin_financial_refresh_run(date, timestamptz) to service_role;
grant execute on function public.finish_financial_refresh_run(date) to service_role;
