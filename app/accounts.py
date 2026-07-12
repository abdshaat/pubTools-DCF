"""Customer login (GitHub via Supabase Auth) and self-service API keys.

A human's browser login session is a distinct credential class from the
machine `X-API-Key` auth in app/auth.py and SupabaseAPIKeyAuthenticator in
app/supabase.py: a login session cookie is never accepted as an API key, and
an API key is never accepted as a login session. This module only ever
produces/consumes the cookies defined below; it never touches `X-API-Key`.

pubTools account model (Phase 6): a login identity (a Supabase Auth user) is
distinct from an `api_customers` row (the billing/quota entity), which is
distinct from an `api_keys` row. v1 scope: one login owns exactly one customer
record (enforced by a unique constraint on `api_customers.auth_user_id`).
"""

import base64
import hashlib
import os
import secrets
import string
from dataclasses import dataclass
from typing import Any

from fastapi import Request, Response

from .auth import VALUATION_SCOPE, APIKeyAuthenticator
from .supabase import AuthSession, SupabaseAuthClient, SupabaseAuthError, SupabaseClient

SESSION_COOKIE = "pt_session"
REFRESH_COOKIE = "pt_refresh"
OAUTH_VERIFIER_COOKIE = "pt_oauth_verifier"

OAUTH_COOKIE_MAX_AGE = 600  # 10 minutes to complete the GitHub redirect round trip
REFRESH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

LOGIN_ATTEMPTS_DAILY_LIMIT = 20

SELF_SERVICE_SCOPES = (VALUATION_SCOPE,)
SELF_SERVICE_DEFAULT_DAILY_QUOTA = 100
MAX_SELF_SERVICE_KEYS_PER_CUSTOMER = 5


class AccountAuthError(Exception):
    """No valid login session (missing or expired)."""


class AccountKeyNotFoundError(Exception):
    """The referenced API key doesn't exist, or doesn't belong to this customer."""


class AccountLimitError(Exception):
    """A self-service action exceeds an account limit."""


@dataclass(frozen=True)
class CustomerAccount:
    customer_id: str
    auth_user_id: str
    email: str | None
    name: str


def public_base_url() -> str:
    return os.environ.get("PUBLIC_BASE_URL", "http://127.0.0.1:8000").rstrip("/")


def _cookies_secure() -> bool:
    return public_base_url().startswith("https://")


def generate_pkce_pair() -> tuple[str, str]:
    """Returns (code_verifier, code_challenge) per RFC 7636 (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def set_oauth_verifier_cookie(response: Response, *, verifier: str) -> None:
    response.set_cookie(
        OAUTH_VERIFIER_COOKIE,
        verifier,
        max_age=OAUTH_COOKIE_MAX_AGE,
        httponly=True,
        secure=_cookies_secure(),
        samesite="lax",
    )


def clear_oauth_verifier_cookie(response: Response) -> None:
    response.delete_cookie(OAUTH_VERIFIER_COOKIE)


def set_session_cookies(response: Response, session: AuthSession) -> None:
    secure = _cookies_secure()
    response.set_cookie(
        SESSION_COOKIE,
        session.access_token,
        max_age=session.expires_in,
        httponly=True,
        secure=secure,
        samesite="lax",
    )
    response.set_cookie(
        REFRESH_COOKIE,
        session.refresh_token,
        max_age=REFRESH_COOKIE_MAX_AGE,
        httponly=True,
        secure=secure,
        samesite="lax",
    )


def clear_session_cookies(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE)
    response.delete_cookie(REFRESH_COOKIE)


def build_github_login(auth_client: SupabaseAuthClient) -> tuple[str, str]:
    """Returns (authorize_url, code_verifier) for the login route to hand to
    the browser and stash in a short-lived cookie."""
    verifier, challenge = generate_pkce_pair()
    redirect_to = f"{public_base_url()}/v1/auth/callback"
    url = auth_client.authorize_url(redirect_to=redirect_to, code_challenge=challenge)
    return url, verifier


def _display_name(user: dict[str, Any], session: AuthSession) -> str:
    metadata = user.get("user_metadata") or {}
    return str(
        metadata.get("user_name") or metadata.get("full_name") or session.email or session.user_id
    )


async def _ensure_customer(
    supabase_client: SupabaseClient, *, session: AuthSession, user: dict[str, Any]
) -> CustomerAccount:
    existing = await supabase_client.get_customer_by_auth_user_id(session.user_id)
    if existing is not None:
        return CustomerAccount(
            customer_id=str(existing["id"]),
            auth_user_id=session.user_id,
            email=existing.get("email"),
            name=str(existing.get("name") or ""),
        )

    name = _display_name(user, session)
    created = await supabase_client.create_customer(
        auth_user_id=session.user_id, name=name, email=session.email
    )
    account = CustomerAccount(
        customer_id=str(created["id"]), auth_user_id=session.user_id, email=session.email, name=name
    )
    await supabase_client.record_audit_event(
        customer_id=account.customer_id,
        api_key_id=None,
        action="account.signup",
        metadata={"provider": "github"},
    )
    return account


async def complete_github_login(
    *,
    auth_client: SupabaseAuthClient,
    supabase_client: SupabaseClient,
    request: Request,
    response: Response,
    code: str,
) -> CustomerAccount:
    """Validates the OAuth callback, exchanges the code, provisions the
    customer record on first login, and sets session cookies on `response`.

    No `state` round trip is checked here: Supabase Auth manages its own
    `state` internally between itself and the provider (see
    SupabaseAuthClient.authorize_url), so there is nothing of ours to compare
    it against. The `code_verifier` cookie is what actually protects this
    exchange -- PKCE makes the token exchange fail unless the verifier
    matches the challenge the code was issued for.
    """
    verifier = request.cookies.get(OAUTH_VERIFIER_COOKIE)
    clear_oauth_verifier_cookie(response)
    if not verifier:
        raise AccountAuthError("invalid or expired sign-in attempt")

    session = await auth_client.exchange_code(auth_code=code, code_verifier=verifier)
    user = await auth_client.get_user(access_token=session.access_token)
    account = await _ensure_customer(supabase_client, session=session, user=user)

    set_session_cookies(response, session)
    await supabase_client.record_audit_event(
        customer_id=account.customer_id,
        api_key_id=None,
        action="account.login",
        metadata={"provider": "github"},
    )
    return account


async def _account_from_user(
    supabase_client: SupabaseClient, user: dict[str, Any]
) -> CustomerAccount:
    auth_user_id = str(user["id"])
    existing = await supabase_client.get_customer_by_auth_user_id(auth_user_id)
    if existing is None:
        raise AccountAuthError("no account linked to this session")
    return CustomerAccount(
        customer_id=str(existing["id"]),
        auth_user_id=auth_user_id,
        email=existing.get("email"),
        name=str(existing.get("name") or ""),
    )


async def get_current_customer(
    *, auth_client: SupabaseAuthClient, supabase_client: SupabaseClient, request: Request
) -> tuple[CustomerAccount, AuthSession | None]:
    """Returns (account, refreshed_session). `refreshed_session` is not None
    only when the access token was silently refreshed, in which case the
    caller must set new session cookies on whatever response it returns."""
    access_token = request.cookies.get(SESSION_COOKIE)
    if access_token:
        try:
            user = await auth_client.get_user(access_token=access_token)
            return await _account_from_user(supabase_client, user), None
        except SupabaseAuthError:
            pass

    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if not refresh_token:
        raise AccountAuthError("not signed in")

    try:
        session = await auth_client.refresh(refresh_token=refresh_token)
    except SupabaseAuthError as exc:
        raise AccountAuthError("session expired") from exc

    user = await auth_client.get_user(access_token=session.access_token)
    return await _account_from_user(supabase_client, user), session


# --- self-service API keys ---


def _generate_prefix() -> str:
    alphabet = string.ascii_lowercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(8))


async def list_keys(supabase_client: SupabaseClient, *, customer_id: str) -> list[dict[str, Any]]:
    return await supabase_client.list_customer_keys(customer_id)


async def create_key(
    supabase_client: SupabaseClient, *, customer_id: str, label: str | None
) -> tuple[dict[str, Any], str]:
    existing = await supabase_client.list_customer_keys(customer_id)
    active = [row for row in existing if not row.get("revoked")]
    if len(active) >= MAX_SELF_SERVICE_KEYS_PER_CUSTOMER:
        raise AccountLimitError(
            f"maximum of {MAX_SELF_SERVICE_KEYS_PER_CUSTOMER} active API keys per account"
        )

    prefix = _generate_prefix()
    full_key = f"dcf_{prefix}_{secrets.token_urlsafe(32)}"
    record = await supabase_client.create_customer_key(
        customer_id=customer_id,
        prefix=prefix,
        secret_hash=APIKeyAuthenticator.hash_secret(full_key),
        scopes=list(SELF_SERVICE_SCOPES),
        daily_quota=SELF_SERVICE_DEFAULT_DAILY_QUOTA,
        label=label,
    )
    await supabase_client.record_audit_event(
        customer_id=customer_id,
        api_key_id=str(record["id"]),
        action="account.key_created",
        metadata={"prefix": prefix},
    )
    return record, full_key


async def revoke_key(supabase_client: SupabaseClient, *, customer_id: str, key_id: str) -> None:
    updated = await supabase_client.revoke_customer_key(customer_id=customer_id, key_id=key_id)
    if updated is None:
        raise AccountKeyNotFoundError("API key not found")
    await supabase_client.record_audit_event(
        customer_id=customer_id,
        api_key_id=key_id,
        action="account.key_revoked",
        metadata={},
    )
