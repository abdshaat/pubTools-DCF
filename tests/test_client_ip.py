"""Client-address resolution and the per-IP login cap (Phase 11 Slice 4).

Closes the 2026-07-12 security-review finding. Two failure modes matter and
they pull in opposite directions: trusting the socket peer behind a proxy makes
the whole internet share one bucket, while trusting `X-Forwarded-For` blindly
lets any caller mint a fresh bucket per request. The tests below pin both ends.
"""

import httpx
import pytest
from fastapi.testclient import TestClient

from app.accounts import CSRF_COOKIE, CSRF_HEADER, LOGIN_ATTEMPTS_DAILY_LIMIT
from app.api import create_app
from app.client_ip import (
    SOURCE_FORWARDED,
    SOURCE_PEER,
    SOURCE_UNKNOWN,
    UNKNOWN_CLIENT,
    resolve_client_identity,
)
from app.providers.fmp import FMPClient
from app.settings import Settings
from app.supabase import SupabaseAuthClient, SupabaseClient, SupabaseConfig
from tests.fake_supabase import FakeSupabaseBackend
from tests.test_data_layer import fixture_transport


def resolve(peer, forwarded, hops):
    return resolve_client_identity(peer=peer, forwarded_for=forwarded, trusted_proxy_hops=hops)


# ---------------------------------------------------------------------------
# Resolution rules
# ---------------------------------------------------------------------------


def test_without_trusted_proxies_the_header_is_ignored():
    identity = resolve("10.0.0.9", "203.0.113.7", hops=0)
    assert identity.address == "10.0.0.9"
    assert identity.source == SOURCE_PEER


def test_one_trusted_hop_uses_the_address_the_proxy_appended():
    identity = resolve("10.0.0.9", "203.0.113.7", hops=1)
    assert identity.address == "203.0.113.7"
    assert identity.source == SOURCE_FORWARDED


def test_a_spoofed_prefix_cannot_buy_a_fresh_quota():
    """The caller controls everything left of what the trusted proxy appended."""
    identity = resolve("10.0.0.9", "1.1.1.1, 2.2.2.2, 203.0.113.7", hops=1)
    assert identity.address == "203.0.113.7"

    # ...and inventing a different prefix resolves to the same caller, so the
    # rate-limit bucket does not change.
    again = resolve("10.0.0.9", "9.9.9.9, 203.0.113.7", hops=1)
    assert again.address == identity.address


def test_two_trusted_hops_skip_the_inner_proxy():
    identity = resolve("10.0.0.9", "203.0.113.7, 10.1.1.1", hops=2)
    assert identity.address == "203.0.113.7"


def test_a_chain_shorter_than_the_configured_hops_is_discarded():
    """The request did not arrive the way the configuration claims."""
    identity = resolve("10.0.0.9", "203.0.113.7", hops=2)
    assert identity.address == "10.0.0.9"
    assert identity.source == SOURCE_PEER


@pytest.mark.parametrize(
    ("forwarded", "expected"),
    [
        ("203.0.113.7:51234", "203.0.113.7"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("[2001:db8::1]:443", "2001:db8::1"),
        ("2001:db8::1", "2001:db8::1"),
        ("  203.0.113.7  ", "203.0.113.7"),
    ],
)
def test_entries_are_normalized_so_one_caller_is_one_bucket(forwarded, expected):
    assert resolve("10.0.0.9", forwarded, hops=1).address == expected


def test_empty_and_malformed_chains_fall_back_to_the_peer():
    assert resolve("10.0.0.9", "", hops=1).address == "10.0.0.9"
    assert resolve("10.0.0.9", " , , ", hops=1).address == "10.0.0.9"
    assert resolve("10.0.0.9", None, hops=1).address == "10.0.0.9"


def test_a_missing_peer_is_reported_rather_than_guessed():
    identity = resolve(None, None, hops=0)
    assert identity.address == UNKNOWN_CLIENT
    assert identity.source == SOURCE_UNKNOWN


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_trust_defaults_to_nothing_off_platform():
    assert Settings.from_env().trusted_proxy_hops == 0


def test_trust_defaults_to_one_hop_on_vercel(monkeypatch):
    """Vercel puts exactly one proxy in front of the function."""
    monkeypatch.setenv("VERCEL", "1")
    assert Settings.from_env().trusted_proxy_hops == 1


def test_hop_count_is_configurable_and_validated(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "2")
    assert Settings.from_env().trusted_proxy_hops == 2

    monkeypatch.setenv("TRUSTED_PROXY_HOPS", "-1")
    with pytest.raises(Exception, match="TRUSTED_PROXY_HOPS"):
        Settings.from_env()


# ---------------------------------------------------------------------------
# End to end: the login cap
# ---------------------------------------------------------------------------


def _login_app(monkeypatch, hops: int):
    monkeypatch.setenv("TRUSTED_PROXY_HOPS", str(hops))
    backend = FakeSupabaseBackend()
    config = SupabaseConfig(url="https://example.supabase.co", service_role_key="service-key")
    return create_app(
        fmp_client=FMPClient(api_key="test-key", transport=fixture_transport()),
        supabase_client=SupabaseClient(config, transport=backend.transport()),
        auth_client=SupabaseAuthClient(config, transport=backend.transport()),
    )


def _attempt(client: TestClient, forwarded: str | None) -> httpx.Response:
    # The CSRF gate runs before the login limiter, so a valid token is needed
    # to reach the thing under test at all.
    client.get("/dcf")
    headers = {CSRF_HEADER: client.cookies[CSRF_COOKIE]}
    if forwarded:
        headers["X-Forwarded-For"] = forwarded
    return client.post(
        "/v1/auth/email/login", json={"email": "person@example.com"}, headers=headers
    )


def test_distinct_callers_behind_a_proxy_get_their_own_login_budget(monkeypatch):
    with TestClient(_login_app(monkeypatch, hops=1)) as client:
        for _ in range(LOGIN_ATTEMPTS_DAILY_LIMIT):
            assert _attempt(client, "203.0.113.7").status_code != 429
        # That caller is now capped...
        assert _attempt(client, "203.0.113.7").status_code == 429
        # ...while a different caller through the same proxy is unaffected.
        assert _attempt(client, "198.51.100.4").status_code != 429


def test_a_capped_caller_cannot_escape_by_forging_the_header(monkeypatch):
    with TestClient(_login_app(monkeypatch, hops=1)) as client:
        for _ in range(LOGIN_ATTEMPTS_DAILY_LIMIT):
            _attempt(client, "203.0.113.7")
        assert _attempt(client, "203.0.113.7").status_code == 429
        # Everything left of the proxy-appended entry is caller-controlled and
        # must not open a new bucket.
        assert _attempt(client, "1.2.3.4, 203.0.113.7").status_code == 429
        assert _attempt(client, "8.8.8.8, 9.9.9.9, 203.0.113.7").status_code == 429


def test_headers_are_ignored_when_no_proxy_is_trusted(monkeypatch):
    """Off-platform, every caller shares the peer identity of the test client."""
    with TestClient(_login_app(monkeypatch, hops=0)) as client:
        for _ in range(LOGIN_ATTEMPTS_DAILY_LIMIT):
            _attempt(client, "203.0.113.7")
        # A forged header buys nothing because nothing is trusted.
        assert _attempt(client, "198.51.100.4").status_code == 429
