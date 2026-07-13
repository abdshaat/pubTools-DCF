"""Customer login (GitHub OAuth or email magic link via Supabase Auth) and
self-service API keys.

A human's browser login session is a distinct credential class from the
machine `X-API-Key` auth in app/auth.py and SupabaseAPIKeyAuthenticator in
app/supabase.py: a login session cookie is never accepted as an API key, and
an API key is never accepted as a login session. This module only ever
produces/consumes the cookies defined below; it never touches `X-API-Key`.

Both login methods land on the same PKCE code-exchange completion
(`complete_login`): GitHub's authorize redirect and Supabase's magic-link
`/auth/v1/verify` both redirect the browser to `{PUBLIC_BASE_URL}/v1/auth/
callback?code=...`, exchanged the same way regardless of which provider
issued it. Nothing provider-specific happens after the code exchange except
recording which provider was used, read back from Supabase's own
`user.app_metadata.provider`.

pubTools account model (Phase 6): a login identity (a Supabase Auth user) is
distinct from an `api_customers` row (the billing/quota entity), which is
distinct from an `api_keys` row. v1 scope: one login owns exactly one customer
record (enforced by a unique constraint on `api_customers.auth_user_id`); a
customer who signs in with both GitHub and email gets two separate accounts
today -- Supabase identity linking across providers is not wired up (deferred,
see project-docs/IMPLEMENTATION_PLAN.md Phase 6).
"""

import asyncio
import base64
import hashlib
import hmac
import os
import re
import secrets
import string
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import Request, Response

from .auth import VALUATION_SCOPE, APIKeyAuthenticator
from .supabase import AuthSession, SupabaseAuthClient, SupabaseAuthError, SupabaseClient

_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SESSION_COOKIE = "pt_session"
REFRESH_COOKIE = "pt_refresh"
OAUTH_VERIFIER_COOKIE = "pt_oauth_verifier"
CSRF_COOKIE = "pt_csrf"
CSRF_HEADER = "X-CSRF-Token"

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


class InvalidEmailError(Exception):
    """The submitted email address is not well-formed."""


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


def generate_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def set_csrf_cookie(response: Response, *, token: str | None = None) -> str:
    token = token or generate_csrf_token()
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=REFRESH_COOKIE_MAX_AGE,
        httponly=False,
        secure=_cookies_secure(),
        samesite="lax",
    )
    return token


def clear_csrf_cookie(response: Response) -> None:
    response.delete_cookie(CSRF_COOKIE)


def csrf_tokens_match(*, cookie_token: str | None, header_token: str | None) -> bool:
    if not cookie_token or not header_token:
        return False
    return hmac.compare_digest(cookie_token, header_token)


def build_github_login(auth_client: SupabaseAuthClient) -> tuple[str, str]:
    """Returns (authorize_url, code_verifier) for the login route to hand to
    the browser and stash in a short-lived cookie."""
    verifier, challenge = generate_pkce_pair()
    redirect_to = f"{public_base_url()}/v1/auth/callback"
    url = auth_client.authorize_url(redirect_to=redirect_to, code_challenge=challenge)
    return url, verifier


def is_valid_email(email: str) -> bool:
    return bool(_EMAIL_PATTERN.match(email)) and len(email) <= 254


async def request_email_login(auth_client: SupabaseAuthClient, *, email: str) -> str:
    """Sends a magic-link sign-in email; returns the code_verifier for the
    caller to stash in the same short-lived cookie the GitHub flow uses.

    Always succeeds from the caller's point of view for a well-formed email,
    even if the address doesn't exist -- Supabase itself doesn't reveal
    whether an account exists, and neither do we.
    """
    if not is_valid_email(email):
        raise InvalidEmailError("must be a valid email address")
    verifier, challenge = generate_pkce_pair()
    redirect_to = f"{public_base_url()}/v1/auth/callback"
    await auth_client.request_magic_link(
        email=email, redirect_to=redirect_to, code_challenge=challenge
    )
    return verifier


def _display_name(user: dict[str, Any], session: AuthSession) -> str:
    metadata = user.get("user_metadata") or {}
    return str(
        metadata.get("user_name") or metadata.get("full_name") or session.email or session.user_id
    )


def _provider_of(user: dict[str, Any]) -> str:
    app_metadata = user.get("app_metadata") or {}
    return str(app_metadata.get("provider") or "unknown")


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
        metadata={"provider": _provider_of(user)},
    )
    return account


async def complete_login(
    *,
    auth_client: SupabaseAuthClient,
    supabase_client: SupabaseClient,
    request: Request,
    response: Response,
    code: str,
) -> CustomerAccount:
    """Completes either login method (GitHub OAuth or email magic link) --
    both redirect the browser to this same code-exchange step. Provisions the
    customer record on first login and sets session cookies on `response`.

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
    set_csrf_cookie(response)
    await supabase_client.record_audit_event(
        customer_id=account.customer_id,
        api_key_id=None,
        action="account.login",
        metadata={"provider": _provider_of(user)},
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


def _today_window() -> str:
    return datetime.now(UTC).date().isoformat()


async def _with_usage_today(supabase_client: SupabaseClient, row: dict[str, Any]) -> dict[str, Any]:
    """Attaches `requests_used_today` to a copy of a key row. Revoked keys
    don't consume quota anymore, so their usage isn't looked up."""
    enriched = dict(row)
    if row.get("revoked"):
        enriched["requests_used_today"] = None
        return enriched
    enriched["requests_used_today"] = await supabase_client.get_daily_quota_usage(
        subject_id=str(row["id"]), window=_today_window()
    )
    return enriched


async def list_keys(supabase_client: SupabaseClient, *, customer_id: str) -> list[dict[str, Any]]:
    rows = await supabase_client.list_customer_keys(customer_id)
    return list(await asyncio.gather(*(_with_usage_today(supabase_client, row) for row in rows)))


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
    # brand new key: no requests have been made against it yet, no lookup needed
    record = {**record, "requests_used_today": 0}
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


async def rotate_key(
    supabase_client: SupabaseClient, *, customer_id: str, key_id: str
) -> tuple[dict[str, Any], str]:
    """Regenerates a key's secret in place -- same id/prefix/label/created_at,
    only the secret (and its hash) change. Revoked keys can't be rotated
    (rotating one back to a usable state would be surprising and isn't
    supported -- create a new key instead); this looks like a generic
    not-found to the caller, same as revoking someone else's key does, so
    neither leaks *why* the action was rejected.
    """
    existing = await supabase_client.list_customer_keys(customer_id)
    match = next((row for row in existing if str(row["id"]) == key_id), None)
    if match is None or match.get("revoked"):
        raise AccountKeyNotFoundError("API key not found")

    full_key = f"dcf_{match['prefix']}_{secrets.token_urlsafe(32)}"
    updated = await supabase_client.rotate_customer_key(
        customer_id=customer_id,
        key_id=key_id,
        secret_hash=APIKeyAuthenticator.hash_secret(full_key),
    )
    if updated is None:
        raise AccountKeyNotFoundError("API key not found")

    await supabase_client.record_audit_event(
        customer_id=customer_id,
        api_key_id=key_id,
        action="account.key_rotated",
        metadata={"prefix": match["prefix"]},
    )
    enriched = await _with_usage_today(supabase_client, updated)
    return enriched, full_key


async def rename_key(
    supabase_client: SupabaseClient, *, customer_id: str, key_id: str, label: str | None
) -> dict[str, Any]:
    """Changes a key's label only -- never its secret, scope, or quota.
    Revoked keys can't be renamed (same not-found-shaped rejection as
    rotating one), consistent with the UI hiding all actions on revoked rows.
    """
    updated = await supabase_client.rename_customer_key(
        customer_id=customer_id, key_id=key_id, label=label
    )
    if updated is None:
        raise AccountKeyNotFoundError("API key not found")
    await supabase_client.record_audit_event(
        customer_id=customer_id,
        api_key_id=key_id,
        action="account.key_renamed",
        metadata={"label": label},
    )
    return await _with_usage_today(supabase_client, updated)
