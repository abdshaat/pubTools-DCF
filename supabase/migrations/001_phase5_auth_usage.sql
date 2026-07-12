-- Phase 5: API customers, hashed API keys, daily quotas, usage events, and audit logs.
--
-- Run this in the Supabase SQL editor for the project that backs the Vercel
-- deployment. The application uses the service-role key server-side only; do
-- not expose that key to browsers or commit it to the repository.

create table if not exists public.api_customers (
    id uuid primary key default gen_random_uuid(),
    name text not null,
    created_at timestamptz not null default now()
);

create table if not exists public.api_keys (
    id uuid primary key default gen_random_uuid(),
    customer_id uuid not null references public.api_customers(id) on delete cascade,
    prefix text not null unique,
    secret_hash text not null,
    scopes text[] not null default array['valuation:read'],
    revoked boolean not null default false,
    expires_at timestamptz,
    daily_quota integer not null default 100 check (daily_quota > 0),
    created_at timestamptz not null default now(),
    last_used_at timestamptz
);

create index if not exists api_keys_customer_id_idx on public.api_keys(customer_id);
create index if not exists api_keys_prefix_idx on public.api_keys(prefix);

create table if not exists public.daily_quota_counters (
    subject_id text not null,
    quota_window date not null,
    request_count integer not null default 0 check (request_count >= 0),
    updated_at timestamptz not null default now(),
    primary key (subject_id, quota_window)
);

create table if not exists public.usage_events (
    id bigint generated always as identity primary key,
    request_id uuid not null,
    customer_id uuid references public.api_customers(id) on delete set null,
    api_key_id uuid references public.api_keys(id) on delete set null,
    method text not null,
    path text not null,
    ticker text,
    status_code integer not null,
    quota_consumed boolean not null,
    rate_limited boolean not null,
    recorded_at timestamptz not null default now()
);

create index if not exists usage_events_customer_recorded_idx
    on public.usage_events(customer_id, recorded_at desc);
create index if not exists usage_events_api_key_recorded_idx
    on public.usage_events(api_key_id, recorded_at desc);
create index if not exists usage_events_request_id_idx on public.usage_events(request_id);

create table if not exists public.audit_events (
    id bigint generated always as identity primary key,
    customer_id uuid references public.api_customers(id) on delete set null,
    api_key_id uuid references public.api_keys(id) on delete set null,
    action text not null,
    metadata jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now()
);

alter table public.api_customers enable row level security;
alter table public.api_keys enable row level security;
alter table public.daily_quota_counters enable row level security;
alter table public.usage_events enable row level security;
alter table public.audit_events enable row level security;

create or replace function public.consume_daily_quota(
    p_subject_id text,
    p_limit integer,
    p_window date
)
returns table (
    allowed boolean,
    "limit" integer,
    remaining integer,
    reset_epoch bigint,
    retry_after integer
)
language plpgsql
security definer
set search_path = public
as $$
declare
    new_count integer;
    reset_at timestamptz;
begin
    if p_subject_id is null or length(trim(p_subject_id)) = 0 then
        raise exception 'quota subject is required';
    end if;
    if p_limit is null or p_limit < 1 then
        raise exception 'quota limit must be positive';
    end if;
    if p_window is null then
        raise exception 'quota window is required';
    end if;

    insert into public.daily_quota_counters (subject_id, quota_window, request_count)
    values (p_subject_id, p_window, 1)
    on conflict (subject_id, quota_window)
    do update set
        request_count = public.daily_quota_counters.request_count + 1,
        updated_at = now()
    returning request_count into new_count;

    reset_at := ((p_window + 1)::timestamp at time zone 'UTC');

    return query select
        new_count <= p_limit as allowed,
        p_limit as "limit",
        greatest(p_limit - new_count, 0) as remaining,
        extract(epoch from reset_at)::bigint as reset_epoch,
        greatest(1, ceil(extract(epoch from reset_at - now()))::integer) as retry_after;
end;
$$;

create or replace function public.record_usage_event(p_event jsonb)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    insert into public.usage_events (
        request_id,
        customer_id,
        api_key_id,
        method,
        path,
        ticker,
        status_code,
        quota_consumed,
        rate_limited,
        recorded_at
    )
    values (
        (p_event ->> 'request_id')::uuid,
        nullif(p_event ->> 'customer_id', '')::uuid,
        nullif(p_event ->> 'api_key_id', '')::uuid,
        p_event ->> 'method',
        p_event ->> 'path',
        nullif(p_event ->> 'ticker', ''),
        (p_event ->> 'status_code')::integer,
        coalesce((p_event ->> 'quota_consumed')::boolean, false),
        coalesce((p_event ->> 'rate_limited')::boolean, false),
        coalesce((p_event ->> 'recorded_at')::timestamptz, now())
    );
end;
$$;

-- PostgREST exposes every public-schema function as an RPC endpoint, and
-- Postgres grants EXECUTE on new functions to PUBLIC by default. Without this,
-- anyone holding the project's anon/authenticated key could call these
-- SECURITY DEFINER functions directly over HTTP, bypassing app-layer API-key
-- auth entirely. Only the service role (used server-side by this app) may
-- call them.
revoke execute on function public.consume_daily_quota(text, integer, date) from public;
revoke execute on function public.record_usage_event(jsonb) from public;
grant execute on function public.consume_daily_quota(text, integer, date) to service_role;
grant execute on function public.record_usage_event(jsonb) to service_role;
