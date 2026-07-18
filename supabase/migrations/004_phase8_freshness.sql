-- Phase 8 Slice C: make daily-refresh state customer-visible through the
-- ticker head. Run after 003_phase8_snapshots.sql.
--
-- Rollback:
--   drop function public.complete_financial_refresh_claim(text, date, text, text);
--   Re-apply the 003 definitions of begin_financial_refresh_run() and
--   finish_financial_refresh_run() if the status propagation must be removed.

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

    update public.ticker_snapshot_heads as head
    set last_refresh_attempt_at = now(),
        refresh_status = 'daily_refresh_running',
        updated_at = now()
    from public.financial_refresh_claims as claim
    where claim.refresh_date = p_refresh_date
      and claim.ticker = head.ticker;

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

-- Claim completion and failed-head publication are one transaction. A
-- successful provider cycle already publishes current status through
-- store_ticker_snapshot() before this RPC is called.
create or replace function public.complete_financial_refresh_claim(
    p_ticker text,
    p_refresh_date date,
    p_status text,
    p_error_code text
)
returns void
language plpgsql
security definer
set search_path = public
as $$
begin
    if p_status not in ('succeeded', 'failed') then
        raise exception 'invalid refresh claim status';
    end if;

    update public.financial_refresh_claims
    set completed_at = now(),
        status = p_status,
        error_code = p_error_code
    where ticker = upper(p_ticker)
      and refresh_date = p_refresh_date;

    if not found then
        raise exception 'refresh claim not found';
    end if;

    if p_status = 'failed' then
        update public.ticker_snapshot_heads
        set last_refresh_attempt_at = now(),
            refresh_status = 'daily_refresh_failed',
            updated_at = now()
        where ticker = upper(p_ticker);
    end if;
end;
$$;

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

    update public.ticker_snapshot_heads as head
    set refresh_status = case
            when claim.status = 'failed' then 'daily_refresh_failed'
            else 'daily_refresh_partial_failed'
        end,
        updated_at = now()
    from public.financial_refresh_claims as claim
    where claim.refresh_date = p_refresh_date
      and claim.ticker = head.ticker
      and claim.status <> 'succeeded';

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

revoke execute on function public.complete_financial_refresh_claim(text, date, text, text)
    from public;
grant execute on function public.complete_financial_refresh_claim(text, date, text, text)
    to service_role;

