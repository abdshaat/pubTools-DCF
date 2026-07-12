"""Unit tests for app/accounts.py: PKCE, login URL building, and self-service
API-key management. Route-level login/callback/session tests live in
tests/test_api.py alongside the other HTTP-layer tests.
"""

import asyncio
import base64
import hashlib
from datetime import UTC, datetime

import pytest
from starlette.requests import Request

from app.accounts import (
    MAX_SELF_SERVICE_KEYS_PER_CUSTOMER,
    SELF_SERVICE_DEFAULT_DAILY_QUOTA,
    AccountAuthError,
    AccountKeyNotFoundError,
    AccountLimitError,
    InvalidEmailError,
    build_github_login,
    create_key,
    generate_pkce_pair,
    get_current_customer,
    is_valid_email,
    list_keys,
    request_email_login,
    revoke_key,
    rotate_key,
)
from app.auth import APIKeyAuthenticator
from app.supabase import SupabaseAuthClient, SupabaseClient, SupabaseConfig
from tests.fake_supabase import FakeSupabaseBackend


def _run(coro):
    return asyncio.run(coro)


def _request_with_cookies(**cookies: str) -> Request:
    header = "; ".join(f"{name}={value}" for name, value in cookies.items())
    scope = {"type": "http", "headers": [(b"cookie", header.encode())] if cookies else []}
    return Request(scope)


def _config() -> SupabaseConfig:
    return SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key")


def test_generate_pkce_pair_produces_a_valid_s256_challenge():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
    expected = expected.rstrip(b"=").decode("ascii")
    assert challenge == expected
    # no padding, URL-safe alphabet only
    assert "=" not in verifier
    assert "=" not in challenge


def test_build_github_login_url_has_expected_params(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    auth_client = SupabaseAuthClient(_config())
    url, verifier = build_github_login(auth_client)
    assert url.startswith("https://example.supabase.co/auth/v1/authorize?")
    assert "provider=github" in url
    # `state` is deliberately not sent -- Supabase Auth manages it internally
    # and a caller-supplied value breaks its own callback validation
    # (bad_oauth_state).
    assert "state=" not in url
    assert "redirect_to=http%3A%2F%2F127.0.0.1%3A8000%2Fv1%2Fauth%2Fcallback" in url
    assert "code_challenge_method=s256" in url
    assert len(verifier) >= 43


@pytest.mark.parametrize(
    "email,expected",
    [
        ("a@example.com", True),
        ("a.b+tag@example.co.uk", True),
        ("not-an-email", False),
        ("missing-domain@", False),
        ("@missing-local.com", False),
        ("has space@example.com", False),
        ("x" * 250 + "@example.com", False),  # over 254 chars
    ],
)
def test_is_valid_email(email, expected):
    assert is_valid_email(email) is expected


def test_request_email_login_rejects_malformed_email():
    backend = FakeSupabaseBackend()
    auth_client = SupabaseAuthClient(_config(), transport=backend.transport())

    async def exercise():
        with pytest.raises(InvalidEmailError):
            await request_email_login(auth_client, email="not-an-email")
        assert backend.otp_requests == []  # rejected before ever calling Supabase

    _run(exercise())


def test_request_email_login_sends_otp_with_pkce_challenge(monkeypatch):
    monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    backend = FakeSupabaseBackend()
    auth_client = SupabaseAuthClient(_config(), transport=backend.transport())

    async def exercise():
        verifier = await request_email_login(auth_client, email="customer@example.com")
        assert len(verifier) >= 43
        assert len(backend.otp_requests) == 1
        request = backend.otp_requests[0]
        assert request["email"] == "customer@example.com"
        assert request["create_user"] is True
        assert request["code_challenge_method"] == "s256"
        assert request["redirect_to"] == "http://127.0.0.1:8000/v1/auth/callback"
        # the verifier returned to the caller must hash to the sent challenge
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        expected_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        assert request["code_challenge"] == expected_challenge

    _run(exercise())


def test_create_key_defaults_scope_quota_and_records_audit_event():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-1", "name": "alice", "email": "a@example.com", "auth_user_id": "gh-1"}
    )

    async def exercise():
        record, full_key = await create_key(client, customer_id="cust-1", label="my key")
        assert full_key.startswith("dcf_")
        assert record["scopes"] == ["valuation:read"]
        assert record["daily_quota"] == SELF_SERVICE_DEFAULT_DAILY_QUOTA
        assert record["label"] == "my key"
        assert any(e["action"] == "account.key_created" for e in backend.audit_events)

    _run(exercise())


def test_create_key_enforces_active_key_limit_but_ignores_revoked():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-1", "name": "alice", "email": None, "auth_user_id": "gh-1"}
    )

    async def exercise():
        records = []
        for _ in range(MAX_SELF_SERVICE_KEYS_PER_CUSTOMER):
            record, _ = await create_key(client, customer_id="cust-1", label=None)
            records.append(record)

        with pytest.raises(AccountLimitError):
            await create_key(client, customer_id="cust-1", label=None)

        # revoking one frees up a slot
        await revoke_key(client, customer_id="cust-1", key_id=records[0]["id"])
        record, _ = await create_key(client, customer_id="cust-1", label=None)
        assert record["id"] != records[0]["id"]

    _run(exercise())


def test_revoke_key_rejects_wrong_customer():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-a", "name": "alice", "email": None, "auth_user_id": "gh-a"}
    )
    backend.customers.append({"id": "cust-b", "name": "bob", "email": None, "auth_user_id": "gh-b"})

    async def exercise():
        record, _ = await create_key(client, customer_id="cust-a", label=None)
        with pytest.raises(AccountKeyNotFoundError):
            await revoke_key(client, customer_id="cust-b", key_id=record["id"])
        # unaffected by the failed cross-customer attempt
        rows = await list_keys(client, customer_id="cust-a")
        assert rows[0]["revoked"] is False

    _run(exercise())


def test_rotate_key_issues_a_new_secret_that_invalidates_the_old_one():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-1", "name": "alice", "email": None, "auth_user_id": "gh-1"}
    )

    async def exercise():
        record, old_key = await create_key(client, customer_id="cust-1", label="my key")
        old_hash = APIKeyAuthenticator.hash_secret(old_key)

        updated, new_key = await rotate_key(client, customer_id="cust-1", key_id=record["id"])

        assert new_key != old_key
        assert new_key.startswith(f"dcf_{record['prefix']}_")  # same prefix, same key id
        assert updated["id"] == record["id"]
        assert updated["label"] == "my key"  # unrelated fields preserved

        stored = backend.keys[0]
        assert stored["secret_hash"] != old_hash
        assert stored["secret_hash"] == APIKeyAuthenticator.hash_secret(new_key)

        assert any(e["action"] == "account.key_rotated" for e in backend.audit_events)

    _run(exercise())


def test_rotate_key_rejects_wrong_customer():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-a", "name": "alice", "email": None, "auth_user_id": "gh-a"}
    )
    backend.customers.append({"id": "cust-b", "name": "bob", "email": None, "auth_user_id": "gh-b"})

    async def exercise():
        record, old_key = await create_key(client, customer_id="cust-a", label=None)
        with pytest.raises(AccountKeyNotFoundError):
            await rotate_key(client, customer_id="cust-b", key_id=record["id"])
        # unaffected by the failed cross-customer attempt
        assert backend.keys[0]["secret_hash"] == APIKeyAuthenticator.hash_secret(old_key)

    _run(exercise())


def test_rotate_key_rejects_revoked_key():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-1", "name": "alice", "email": None, "auth_user_id": "gh-1"}
    )

    async def exercise():
        record, _ = await create_key(client, customer_id="cust-1", label=None)
        await revoke_key(client, customer_id="cust-1", key_id=record["id"])
        with pytest.raises(AccountKeyNotFoundError):
            await rotate_key(client, customer_id="cust-1", key_id=record["id"])

    _run(exercise())


def test_rotate_key_rejects_nonexistent_key():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-1", "name": "alice", "email": None, "auth_user_id": "gh-1"}
    )

    async def exercise():
        with pytest.raises(AccountKeyNotFoundError):
            await rotate_key(client, customer_id="cust-1", key_id="does-not-exist")

    _run(exercise())


def test_list_keys_returns_only_that_customers_rows():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-a", "name": "alice", "email": None, "auth_user_id": "gh-a"}
    )
    backend.customers.append({"id": "cust-b", "name": "bob", "email": None, "auth_user_id": "gh-b"})

    async def exercise():
        await create_key(client, customer_id="cust-a", label="a-key")
        await create_key(client, customer_id="cust-b", label="b-key")
        rows_a = await list_keys(client, customer_id="cust-a")
        assert len(rows_a) == 1
        assert rows_a[0]["label"] == "a-key"

    _run(exercise())


def test_create_key_returns_zero_requests_used_today_without_a_quota_lookup():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-1", "name": "alice", "email": None, "auth_user_id": "gh-1"}
    )

    async def exercise():
        record, _ = await create_key(client, customer_id="cust-1", label=None)
        assert record["requests_used_today"] == 0
        # freshly created -- no quota-usage lookup should have happened at all
        assert backend.quota_counters == {}

    _run(exercise())


def test_list_keys_enriches_active_keys_with_todays_usage():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-1", "name": "alice", "email": None, "auth_user_id": "gh-1"}
    )

    async def exercise():
        record, _ = await create_key(client, customer_id="cust-1", label=None)
        today = datetime.now(UTC).date().isoformat()
        backend.quota_counters[(record["id"], today)] = 7

        rows = await list_keys(client, customer_id="cust-1")
        assert len(rows) == 1
        assert rows[0]["requests_used_today"] == 7

    _run(exercise())


def test_list_keys_does_not_look_up_usage_for_revoked_keys():
    backend = FakeSupabaseBackend()
    client = SupabaseClient(_config(), transport=backend.transport())
    backend.customers.append(
        {"id": "cust-1", "name": "alice", "email": None, "auth_user_id": "gh-1"}
    )

    async def exercise():
        record, _ = await create_key(client, customer_id="cust-1", label=None)
        await revoke_key(client, customer_id="cust-1", key_id=record["id"])

        rows = await list_keys(client, customer_id="cust-1")
        assert len(rows) == 1
        assert rows[0]["revoked"] is True
        assert rows[0]["requests_used_today"] is None
        # no counter row was ever created/queried for the revoked key
        assert backend.quota_counters == {}

    _run(exercise())


def _linked_clients(backend: FakeSupabaseBackend) -> tuple[SupabaseAuthClient, SupabaseClient]:
    config = _config()
    return (
        SupabaseAuthClient(config, transport=backend.transport()),
        SupabaseClient(config, transport=backend.transport()),
    )


def test_get_current_customer_with_no_cookies_raises():
    backend = FakeSupabaseBackend()
    auth_client, supabase_client = _linked_clients(backend)

    async def exercise():
        with pytest.raises(AccountAuthError):
            await get_current_customer(
                auth_client=auth_client,
                supabase_client=supabase_client,
                request=_request_with_cookies(),
            )

    _run(exercise())


def test_get_current_customer_silently_refreshes_expired_access_token():
    backend = FakeSupabaseBackend()
    auth_client, supabase_client = _linked_clients(backend)
    backend.customers.append(
        {"id": "cust-1", "name": "octocat", "email": "a@example.com", "auth_user_id": "gh-1"}
    )
    user = {"id": "gh-1", "email": "a@example.com", "user_metadata": {"user_name": "octocat"}}
    backend.refreshable["good-refresh"] = user  # access token deliberately never tracked/expired

    async def exercise():
        account, refreshed = await get_current_customer(
            auth_client=auth_client,
            supabase_client=supabase_client,
            request=_request_with_cookies(pt_session="stale", pt_refresh="good-refresh"),
        )
        assert account.customer_id == "cust-1"
        assert refreshed is not None
        assert refreshed.access_token != "stale"

    _run(exercise())


def test_get_current_customer_raises_when_refresh_token_also_invalid():
    backend = FakeSupabaseBackend()
    auth_client, supabase_client = _linked_clients(backend)

    async def exercise():
        with pytest.raises(AccountAuthError):
            await get_current_customer(
                auth_client=auth_client,
                supabase_client=supabase_client,
                request=_request_with_cookies(pt_session="stale", pt_refresh="also-bad"),
            )

    _run(exercise())


def test_get_current_customer_raises_when_no_customer_linked_to_session():
    """A valid Supabase session whose user was never provisioned an
    api_customers row (e.g. deleted server-side) must not authenticate."""
    backend = FakeSupabaseBackend()
    auth_client, supabase_client = _linked_clients(backend)
    user = {"id": "gh-orphan", "email": None, "user_metadata": {}}
    backend.sessions["orphan-access"] = user

    async def exercise():
        with pytest.raises(AccountAuthError):
            await get_current_customer(
                auth_client=auth_client,
                supabase_client=supabase_client,
                request=_request_with_cookies(pt_session="orphan-access"),
            )

    _run(exercise())
