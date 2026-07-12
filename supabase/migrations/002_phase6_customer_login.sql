-- Phase 6: link a customer to a Supabase Auth login (GitHub OAuth) and let
-- self-service keys carry a customer-chosen label.
--
-- Run this in the Supabase SQL editor after 001_phase5_auth_usage.sql. Human
-- login sessions (this migration) are a distinct credential class from the
-- machine API keys added in Phase 5 -- a login session is never usable as an
-- X-API-Key, and vice versa; enforced in application code, not here.
--
-- No new SECURITY DEFINER functions are added, so no new PostgREST RPC
-- surface needs privilege lockdown: customer/key CRUD for the self-service
-- flow goes through plain PostgREST table access using the service-role key
-- only (same posture as the tables already have -- RLS enabled, no policies,
-- ownership enforced in application code).

alter table public.api_customers
    add column if not exists auth_user_id uuid unique references auth.users(id) on delete set null;
alter table public.api_customers
    add column if not exists email text;

create index if not exists api_customers_auth_user_id_idx
    on public.api_customers(auth_user_id);

alter table public.api_keys
    add column if not exists label text;
