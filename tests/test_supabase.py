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


def _consume(limiter, *, identity: str, limit: int):
    """The P3 consume, with the request metadata every caller has to supply."""
    return limiter.consume_and_record(
        identity=identity,
        limit=limit,
        request_id="11111111-1111-1111-1111-111111111111",
        method="GET",
        path="/v1/valuations/AAPL",
        ticker="AAPL",
    )


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
        if request.url.path == "/rest/v1/rpc/consume_daily_quota_and_record":
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
                await _consume(limiter, identity="key-1", limit=100)
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
                await _consume(limiter, identity="key-1", limit=100)
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
            result = await _consume(limiter, identity="key-1", limit=100)
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


def test_get_daily_quota_usage_returns_zero_when_no_counter_row_exists():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[])

    async def exercise() -> None:
        client = SupabaseClient(_config(), transport=httpx.MockTransport(handler))
        try:
            assert await client.get_daily_quota_usage(subject_id="k1", window="2026-07-12") == 0
        finally:
            await client.aclose()

    _run(exercise())


def test_get_daily_quota_usage_returns_the_stored_count():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["subject_id"] == "eq.k1"
        assert request.url.params["quota_window"] == "eq.2026-07-12"
        return httpx.Response(200, json=[{"request_count": 42}])

    async def exercise() -> None:
        client = SupabaseClient(_config(), transport=httpx.MockTransport(handler))
        try:
            assert await client.get_daily_quota_usage(subject_id="k1", window="2026-07-12") == 42
        finally:
            await client.aclose()

    _run(exercise())


def test_get_daily_quota_usage_raises_on_error_or_malformed_payload():
    async def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    async def malformed_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"not": "a list"})

    async def exercise() -> None:
        for handler in (error_handler, malformed_handler):
            client = SupabaseClient(_config(), transport=httpx.MockTransport(handler))
            try:
                with pytest.raises(SupabaseError):
                    await client.get_daily_quota_usage(subject_id="k1", window="2026-07-12")
            finally:
                await client.aclose()

    _run(exercise())


# ---------------------------------------------------------------------------
# Safety/security audit regressions (2026-07-26)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rows", "case"),
    [
        ([{"allowed": True, "limit": 100}], "fields missing from the row"),
        (
            [
                {
                    "allowed": True,
                    "limit": "many",
                    "remaining": 1,
                    "reset_epoch": 1,
                    "retry_after": 1,
                }
            ],
            "a field that will not convert",
        ),
        (
            [{"allowed": True, "limit": None, "remaining": 1, "reset_epoch": 1, "retry_after": 1}],
            "a null where a number belongs",
        ),
    ],
)
def test_unusable_quota_rpc_payload_raises_supabase_error(rows, case):
    """The quota RPC decides whether a request may be served, and the
    middleware fails closed on SupabaseError *only* -- a bare KeyError or
    ValueError from a changed RPC shape used to sail past that handler and
    500 instead of 503."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=rows)

    client = SupabaseClient(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SupabaseError):
        asyncio.run(
            client.consume_daily_quota_and_record(subject_id="k", limit=10, window="2026-07-26")
        )


def test_malformed_expires_at_fails_closed_instead_of_500ing():
    """One bad timestamp in a key row is a storage fault, not an unhandled
    ValueError on the authentication path."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "key-1",
                    "customer_id": "cust-1",
                    "prefix": "abcd1234",
                    "secret_hash": APIKeyAuthenticator.hash_secret("dcf_abcd1234_secret"),
                    "scopes": ["valuation:read"],
                    "revoked": False,
                    "expires_at": "whenever",
                    "daily_quota": 100,
                }
            ],
        )

    client = SupabaseClient(_config(), transport=httpx.MockTransport(handler))
    authenticator = SupabaseAPIKeyAuthenticator(client)
    with pytest.raises(SupabaseError):
        asyncio.run(authenticator.authenticate("dcf_abcd1234_secret"))


def test_unusable_key_row_fields_fail_closed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "id": "key-1",
                    "prefix": "abcd1234",  # customer_id absent
                    "secret_hash": APIKeyAuthenticator.hash_secret("dcf_abcd1234_secret"),
                    "scopes": ["valuation:read"],
                    "revoked": False,
                    "daily_quota": 100,
                }
            ],
        )

    client = SupabaseClient(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(SupabaseError):
        asyncio.run(SupabaseAPIKeyAuthenticator(client).authenticate("dcf_abcd1234_secret"))


def test_last_used_tracking_is_bounded():
    client = SupabaseClient(_config(), transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    authenticator = SupabaseAPIKeyAuthenticator(client)
    for i in range(SupabaseAPIKeyAuthenticator.MAX_TRACKED_KEYS + 100):
        assert authenticator._should_record_use(f"key-{i}") is True
    assert len(authenticator._last_used_writes) == SupabaseAPIKeyAuthenticator.MAX_TRACKED_KEYS


@pytest.mark.parametrize(
    "presented",
    [
        "dcf_ab*cd_secret",  # charset
        "dcf_AB12_secret",  # issued prefixes are lowercase
        "dcf_" + "a" * 33 + "_secret",  # longer than any issued prefix
        "dcf_a.b_secret",  # a PostgREST operator character
        "dcf_" + "a" * 300 + "_secret",  # oversized overall
    ],
)
def test_implausible_key_prefixes_are_refused_before_any_query(presented):
    """The prefix is interpolated into a PostgREST filter to find the
    candidate row. httpx encodes it, so nothing escapes today -- but that puts
    the whole defense on the encoder. Issued prefixes are 8 lowercase
    alphanumerics; anything else never reaches a query."""
    queried: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queried.append(request)
        return httpx.Response(200, json=[])

    client = SupabaseClient(_config(), transport=httpx.MockTransport(handler))
    with pytest.raises(AuthFailure) as exc:
        asyncio.run(SupabaseAPIKeyAuthenticator(client).authenticate(presented))

    assert exc.value.reason is AuthFailureReason.MALFORMED
    assert queried == [], "a refused key must not cost a database round trip"
