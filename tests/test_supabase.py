"""Supabase auth/quota adapter tests over mocked HTTP."""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from app.auth import APIKeyAuthenticator, AuthFailure, AuthFailureReason
from app.supabase import (
    SupabaseAPIKeyAuthenticator,
    SupabaseAuthClient,
    SupabaseAuthError,
    SupabaseClient,
    SupabaseConfig,
    SupabaseDailyQuotaLimiter,
    SupabaseError,
    _parse_datetime,
    _session_from_token_payload,
)


def _run(coro):
    return asyncio.run(coro)


def test_supabase_config_from_env(monkeypatch):
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.delenv("SUPABASE_SERVICE_ROLE_KEY", raising=False)
    assert SupabaseConfig.from_env() is None

    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co/")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-key")
    config = SupabaseConfig.from_env()

    assert config is not None
    assert config.url == "https://example.supabase.co"
    assert config.service_role_key == "service-key"


def test_parse_datetime_handles_utc_z_and_naive_values():
    assert _parse_datetime(None) is None
    assert _parse_datetime("2026-07-11T12:00:00Z").tzinfo is not None
    assert _parse_datetime("2026-07-11T12:00:00").tzinfo == UTC
    with pytest.raises(SupabaseError):
        _parse_datetime(123)


def test_supabase_auth_rejects_revoked_expired_and_insufficient_scope():
    key = "dcf_live_testsecret"
    records = [
        {
            "id": "revoked-key",
            "customer_id": "customer-1",
            "prefix": "revoked",
            "secret_hash": APIKeyAuthenticator.hash_secret("dcf_revoked_testsecret"),
            "scopes": ["valuation:read"],
            "revoked": True,
            "expires_at": None,
            "daily_quota": 100,
        },
        {
            "id": "expired-key",
            "customer_id": "customer-1",
            "prefix": "expired",
            "secret_hash": APIKeyAuthenticator.hash_secret("dcf_expired_testsecret"),
            "scopes": ["valuation:read"],
            "revoked": False,
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
            "daily_quota": 100,
        },
        {
            "id": "scoped-key",
            "customer_id": "customer-1",
            "prefix": "scoped",
            "secret_hash": APIKeyAuthenticator.hash_secret("dcf_scoped_testsecret"),
            "scopes": ["usage:read"],
            "revoked": False,
            "expires_at": None,
            "daily_quota": 100,
        },
        {
            "id": "live-key",
            "customer_id": "customer-1",
            "prefix": "live",
            "secret_hash": APIKeyAuthenticator.hash_secret(key),
            "scopes": "valuation:read",
            "revoked": False,
            "expires_at": None,
            "daily_quota": 100,
        },
    ]

    async def handler(request: httpx.Request) -> httpx.Response:
        prefix = request.url.params["prefix"].removeprefix("eq.")
        return httpx.Response(
            200, json=[record for record in records if record["prefix"] == prefix]
        )

    async def exercise() -> None:
        client = SupabaseClient(
            SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key"),
            transport=httpx.MockTransport(handler),
        )
        auth = SupabaseAPIKeyAuthenticator(client)
        try:
            with pytest.raises(AuthFailure) as revoked:
                await auth.authenticate("dcf_revoked_testsecret")
            assert revoked.value.reason is AuthFailureReason.REVOKED

            with pytest.raises(AuthFailure) as expired:
                await auth.authenticate("dcf_expired_testsecret")
            assert expired.value.reason is AuthFailureReason.EXPIRED

            with pytest.raises(AuthFailure) as scoped:
                await auth.authenticate("dcf_scoped_testsecret")
            assert scoped.value.reason is AuthFailureReason.INSUFFICIENT_SCOPE

            with pytest.raises(SupabaseError):
                await auth.authenticate(key)
        finally:
            await client.aclose()

    _run(exercise())


def test_supabase_lookup_and_quota_malformed_payloads_raise():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/v1/api_keys":
            return httpx.Response(200, json={"not": "a list"})
        if request.url.path == "/rest/v1/rpc/consume_daily_quota":
            return httpx.Response(200, json=["not", "an", "object"])
        raise AssertionError(f"unexpected request: {request.url}")

    async def exercise() -> None:
        client = SupabaseClient(
            SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key"),
            transport=httpx.MockTransport(handler),
        )
        try:
            with pytest.raises(SupabaseError):
                await client.get_api_key_by_prefix("live")
            limiter = SupabaseDailyQuotaLimiter(client)
            with pytest.raises(SupabaseError):
                await limiter.check_and_increment(identity="key-1", limit=100)
        finally:
            await client.aclose()

    _run(exercise())


def test_supabase_quota_rejects_empty_row_array():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def exercise() -> None:
        client = SupabaseClient(
            SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key"),
            transport=httpx.MockTransport(handler),
        )
        try:
            limiter = SupabaseDailyQuotaLimiter(client)
            with pytest.raises(SupabaseError):
                await limiter.check_and_increment(identity="key-1", limit=100)
        finally:
            await client.aclose()

    _run(exercise())


def test_supabase_quota_parses_real_postgrest_table_rpc_shape():
    """`returns table (...)` RPC calls come back as a JSON array of one row,
    not a bare object -- this is the real Supabase/PostgREST response shape."""

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "allowed": True,
                    "limit": 100,
                    "remaining": 99,
                    "reset_epoch": 1_800_000_000,
                    "retry_after": 3600,
                }
            ],
        )

    async def exercise() -> None:
        client = SupabaseClient(
            SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key"),
            transport=httpx.MockTransport(handler),
        )
        try:
            limiter = SupabaseDailyQuotaLimiter(client)
            result = await limiter.check_and_increment(identity="key-1", limit=100)
            assert result.allowed is True
            assert result.limit == 100
            assert result.remaining == 99
        finally:
            await client.aclose()

    _run(exercise())


def _config() -> SupabaseConfig:
    return SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key")


@pytest.mark.parametrize(
    "payload",
    [
        "not-a-dict",
        {"access_token": "a", "refresh_token": "b", "expires_in": 1},  # missing user
        {"user": {"id": "u"}, "access_token": "a", "refresh_token": "b"},  # missing expires_in
    ],
)
def test_session_from_token_payload_rejects_malformed_shapes(payload):
    with pytest.raises(SupabaseAuthError):
        _session_from_token_payload(payload)


def test_supabase_auth_client_error_and_malformed_paths():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/auth/v1/token" and request.url.params["grant_type"] == "pkce":
            return httpx.Response(400, json={"error": "invalid_grant"})
        if request.url.path == "/auth/v1/user":
            return httpx.Response(200, json=["not", "a", "dict"])
        if request.url.path == "/auth/v1/otp":
            return httpx.Response(500)
        raise AssertionError(f"unexpected request: {request.url}")

    async def exercise() -> None:
        client = SupabaseAuthClient(_config(), transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(SupabaseAuthError):
                await client.exchange_code(auth_code="bad", code_verifier="v")
            with pytest.raises(SupabaseAuthError):
                await client.get_user(access_token="whatever")
            with pytest.raises(SupabaseAuthError):
                await client.request_magic_link(
                    email="a@example.com", redirect_to="http://x/callback", code_challenge="c"
                )
        finally:
            await client.aclose()

    _run(exercise())


def test_supabase_client_customer_and_key_error_paths():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/rest/v1/api_customers":
            return httpx.Response(200, json={"not": "a list"})
        if request.method == "POST" and request.url.path == "/rest/v1/api_customers":
            return httpx.Response(200, json=[])
        if request.method == "GET" and request.url.path == "/rest/v1/api_keys":
            return httpx.Response(200, json={"not": "a list"})
        if request.method == "POST" and request.url.path == "/rest/v1/api_keys":
            return httpx.Response(500)
        if request.method == "PATCH" and request.url.path == "/rest/v1/api_keys":
            return httpx.Response(200, json={"not": "a list"})
        if request.url.path == "/rest/v1/audit_events":
            return httpx.Response(500)
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    async def exercise() -> None:
        client = SupabaseClient(_config(), transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(SupabaseError):
                await client.get_customer_by_auth_user_id("u1")
            with pytest.raises(SupabaseError):
                await client.create_customer(auth_user_id="u1", name="n", email=None)
            with pytest.raises(SupabaseError):
                await client.list_customer_keys("c1")
            with pytest.raises(SupabaseError):
                await client.create_customer_key(
                    customer_id="c1",
                    prefix="p",
                    secret_hash="h",
                    scopes=["valuation:read"],
                    daily_quota=100,
                    label=None,
                )
            with pytest.raises(SupabaseError):
                await client.revoke_customer_key(customer_id="c1", key_id="k1")
            with pytest.raises(SupabaseError):
                await client.record_audit_event(
                    customer_id=None, api_key_id=None, action="x", metadata={}
                )
        finally:
            await client.aclose()

    _run(exercise())
