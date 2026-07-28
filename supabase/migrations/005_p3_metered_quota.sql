-- Performance item P3: consume quota and record the usage event in ONE round
-- trip. Run after 004_phase8_freshness.sql.
--
-- Why this needs a migration at all: `consume_daily_quota` runs pre-flight (it
-- decides whether the request may be served at all) while `record_usage_event`
-- ran after the response, because it carried `status_code`. Two RPCs against
-- the same database in the same request, and the second one existed only to
-- learn a number. Folding them means the row is written before the status is
-- known, so `usage_events.status_code` becomes nullable and is filled in later
-- only when the answer is interesting.
--
-- What the column means from here on:
--   429   -- the quota gate rejected the request (known at insert time).
--   NULL  -- the request was admitted and no final status was recorded: a 200
--            in the overwhelming majority of cases, or a request that died
--            before it could finalize. Either way the caller was billed, which
--            is what this ledger is for.
--   other -- the response was not a 200; `finalize_usage_event` recorded it.
--
-- Deliberately additive. `consume_daily_quota` and `record_usage_event` are
-- left exactly as 001 defined them, because this migration must be applied
-- BEFORE the code that uses the new function is deployed, and the code running
-- in production until that deploy still calls both. They can be dropped in a
-- later migration once no deployed instance calls them.
--
-- Rollback:
--   drop function public.finalize_usage_event(uuid, integer);
--   drop function public.consume_daily_quota_and_record(text, integer, date, jsonb);
--   -- Only after backfilling; the NOT NULL cannot be restored while any
--   -- admitted-request row still carries a NULL status:
--   --   update public.usage_events set status_code = 200 where status_code is null;
--   --   alter table public.usage_events alter column status_code set not null;

alter table public.usage_events alter column status_code drop not null;

comment on column public.usage_events.status_code is
    'HTTP status of the metered request. 429 = rejected by the quota gate. '
    'NULL = admitted, final status not recorded (a 200, or a request that died '
    'before finalizing). Any other value was written by finalize_usage_event.';

-- Atomic: the quota increment and the ledger row are one transaction, so
-- billing and the ledger cannot disagree. The previous split could increment
-- the counter and then fail to write the row, charging a request that left no
-- trace.
create or replace function public.consume_daily_quota_and_record(
    p_subject_id text,
    p_limit integer,
    p_window date,
    p_event jsonb default null
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
    v_allowed boolean;
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

    v_allowed := new_count <= p_limit;

    -- The caller supplies only what it knows before serving: who, what, and
    -- which ticker. The outcome fields are derived here from the decision this
    -- function just made, so a caller cannot claim a request was admitted when
    -- the counter says it was rejected.
    if p_event is not null then
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
            case when v_allowed then null else 429 end,
            v_allowed,
            not v_allowed,
            now()
        );
    end if;

    reset_at := ((p_window + 1)::timestamp at time zone 'UTC');

    return query select
        v_allowed as allowed,
        p_limit as "limit",
        greatest(p_limit - new_count, 0) as remaining,
        extract(epoch from reset_at)::bigint as reset_epoch,
        greatest(1, ceil(extract(epoch from reset_at - now()))::integer) as retry_after;
end;
$$;

-- Called only when the response was not a 200, so the common path stays at one
-- round trip. `status_code is null` makes it idempotent and stops a late write
-- from overwriting the 429 the gate already recorded.
create or replace function public.finalize_usage_event(
    p_request_id uuid,
    p_status_code integer
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_request_id is null then
        raise exception 'usage event request id is required';
    end if;
    if p_status_code is null then
        raise exception 'usage event status code is required';
    end if;

    update public.usage_events
    set status_code = p_status_code
    where request_id = p_request_id
      and status_code is null;
end;
$$;

-- Same lockdown as 001: PostgREST exposes every public-schema function as an
-- RPC, and Postgres grants EXECUTE to PUBLIC by default, so anyone holding the
-- anon key could otherwise call these SECURITY DEFINER functions directly and
-- write the metering ledger.
revoke execute on function public.consume_daily_quota_and_record(text, integer, date, jsonb) from public;
revoke execute on function public.finalize_usage_event(uuid, integer) from public;
grant execute on function public.consume_daily_quota_and_record(text, integer, date, jsonb) to service_role;
grant execute on function public.finalize_usage_event(uuid, integer) to service_role;
