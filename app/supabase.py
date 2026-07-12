"""Supabase-backed authentication, quotas, and usage metering.

The app talks to Supabase through its HTTP REST/RPC API using httpx. That keeps
the runtime dependency set small while still giving Vercel functions shared,
durable state for API keys, daily quotas, usage events, and audit logs.

This module also talks to Supabase Auth (GoTrue) for the customer-facing
GitHub sign-in flow (Phase 6). That is a distinct credential class from the
machine `X-API-Key` auth above: a human's login session is never accepted as
an API key, and vice versa. GoTrue calls use the service-role key only as the
required `apikey` header identifying our project to Supabase -- it never
leaves this backend.
"""

import hmac
import os
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlencode

import httpx

from .auth import (
    VALUATION_SCOPE,
    APIKeyAuthenticator,
    AuthenticatedPrincipal,
    AuthFailure,
    AuthFailureReason,
)
from .rate_limit import RateLimitResult


class SupabaseError(Exception):
    """Supabase is unavailable or returned an unexpected response."""


class SupabaseAuthError(SupabaseError):
    """Supabase Auth (GoTrue) rejected the request or returned something we
    can't use: an expired/invalid session, a failed code exchange, etc."""


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    service_role_key: str

    @classmethod
    def from_env(cls) -> "SupabaseConfig | None":
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            return None
        return cls(url=url.rstrip("/"), service_role_key=key)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise SupabaseError("Supabase timestamp field was not a string")
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


class SupabaseClient:
    def __init__(
        self,
        config: SupabaseConfig,
        *,
        timeout: float = 3.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.url,
            timeout=timeout,
            transport=transport,
            headers={
                "apikey": config.service_role_key,
                "authorization": f"Bearer {config.service_role_key}",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_api_key_by_prefix(self, prefix: str) -> dict[str, Any] | None:
        response = await self._client.get(
            "/rest/v1/api_keys",
            params={
                "prefix": f"eq.{prefix}",
                "select": (
                    "id,customer_id,prefix,secret_hash,scopes,revoked,expires_at,daily_quota"
                ),
                "limit": "1",
            },
        )
        if response.status_code >= 500:
            raise SupabaseError("Supabase API-key lookup failed")
        if response.status_code >= 400:
            return None
        payload = response.json()
        if not isinstance(payload, list):
            raise SupabaseError("Supabase API-key lookup returned a non-list payload")
        return payload[0] if payload else None

    async def mark_key_used(self, key_id: str) -> None:
        response = await self._client.patch(
            f"/rest/v1/api_keys?id=eq.{quote(key_id, safe='')}",
            headers={"Prefer": "return=minimal"},
            json={"last_used_at": datetime.now(UTC).isoformat()},
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase last-used update failed")

    async def consume_daily_quota(
        self,
        *,
        subject_id: str,
        limit: int,
        window: str,
    ) -> RateLimitResult:
        response = await self._client.post(
            "/rest/v1/rpc/consume_daily_quota",
            json={
                "p_subject_id": subject_id,
                "p_limit": limit,
                "p_window": window,
            },
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase quota RPC failed")
        rows = response.json()
        # PostgREST returns `returns table (...)` RPC results as a JSON array
        # of rows (even for exactly one row), not a bare object.
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
            raise SupabaseError("Supabase quota RPC returned an unexpected payload shape")
        payload = rows[0]
        return RateLimitResult(
            allowed=bool(payload["allowed"]),
            limit=int(payload["limit"]),
            remaining=int(payload["remaining"]),
            reset_epoch=int(payload["reset_epoch"]),
            retry_after=max(1, int(payload["retry_after"])),
        )

    async def record_usage_event(
        self,
        *,
        event: dict[str, Any],
    ) -> None:
        response = await self._client.post(
            "/rest/v1/rpc/record_usage_event",
            json={"p_event": event},
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase usage-event RPC failed")

    # --- customer accounts / self-service keys (Phase 6) ---

    async def get_customer_by_auth_user_id(self, auth_user_id: str) -> dict[str, Any] | None:
        response = await self._client.get(
            "/rest/v1/api_customers",
            params={
                "auth_user_id": f"eq.{auth_user_id}",
                "select": "id,name,email,auth_user_id",
                "limit": "1",
            },
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase customer lookup failed")
        payload = response.json()
        if not isinstance(payload, list):
            raise SupabaseError("Supabase customer lookup returned a non-list payload")
        return payload[0] if payload else None

    async def create_customer(
        self, *, auth_user_id: str, name: str, email: str | None
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/rest/v1/api_customers",
            headers={"Prefer": "return=representation"},
            json={"auth_user_id": auth_user_id, "name": name, "email": email},
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase customer creation failed")
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise SupabaseError("Supabase customer creation returned an unexpected payload")
        return payload[0]

    async def list_customer_keys(self, customer_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(
            "/rest/v1/api_keys",
            params={
                "customer_id": f"eq.{customer_id}",
                "select": (
                    "id,prefix,label,scopes,daily_quota,revoked,expires_at,created_at,last_used_at"
                ),
                "order": "created_at.desc",
            },
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase key listing failed")
        payload = response.json()
        if not isinstance(payload, list):
            raise SupabaseError("Supabase key listing returned a non-list payload")
        return payload

    async def create_customer_key(
        self,
        *,
        customer_id: str,
        prefix: str,
        secret_hash: str,
        scopes: list[str],
        daily_quota: int,
        label: str | None,
    ) -> dict[str, Any]:
        response = await self._client.post(
            "/rest/v1/api_keys",
            headers={"Prefer": "return=representation"},
            json={
                "customer_id": customer_id,
                "prefix": prefix,
                "secret_hash": secret_hash,
                "scopes": scopes,
                "daily_quota": daily_quota,
                "label": label,
            },
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase key creation failed")
        payload = response.json()
        if not isinstance(payload, list) or not payload:
            raise SupabaseError("Supabase key creation returned an unexpected payload")
        return payload[0]

    async def revoke_customer_key(self, *, customer_id: str, key_id: str) -> dict[str, Any] | None:
        response = await self._client.patch(
            "/rest/v1/api_keys",
            params={
                "id": f"eq.{key_id}",
                "customer_id": f"eq.{customer_id}",
            },
            headers={"Prefer": "return=representation"},
            json={"revoked": True},
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase key revocation failed")
        payload = response.json()
        if not isinstance(payload, list):
            raise SupabaseError("Supabase key revocation returned a non-list payload")
        return payload[0] if payload else None

    async def record_audit_event(
        self,
        *,
        customer_id: str | None,
        api_key_id: str | None,
        action: str,
        metadata: dict[str, Any],
    ) -> None:
        response = await self._client.post(
            "/rest/v1/audit_events",
            headers={"Prefer": "return=minimal"},
            json={
                "customer_id": customer_id,
                "api_key_id": api_key_id,
                "action": action,
                "metadata": metadata,
            },
        )
        if response.status_code >= 400:
            raise SupabaseError("Supabase audit-event write failed")


@dataclass(frozen=True)
class AuthSession:
    access_token: str
    refresh_token: str
    expires_in: int
    user_id: str
    email: str | None
    user_metadata: dict[str, Any]


def _session_from_token_payload(payload: Any) -> AuthSession:
    if not isinstance(payload, dict):
        raise SupabaseAuthError("Supabase auth token endpoint returned an unexpected payload")
    user = payload.get("user")
    access_token = payload.get("access_token")
    refresh_token = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    if not isinstance(user, dict) or "id" not in user:
        raise SupabaseAuthError("Supabase auth token endpoint returned no user")
    if (
        not isinstance(access_token, str)
        or not isinstance(refresh_token, str)
        or not isinstance(expires_in, int)
    ):
        raise SupabaseAuthError("Supabase auth token endpoint returned malformed fields")
    metadata = user.get("user_metadata")
    return AuthSession(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
        user_id=str(user["id"]),
        email=user.get("email"),
        user_metadata=metadata if isinstance(metadata, dict) else {},
    )


class SupabaseAuthClient:
    """Supabase Auth (GoTrue) calls for the GitHub sign-in flow: building the
    authorize URL, exchanging a PKCE code, refreshing, fetching the current
    user, and logout. Never used for the machine `X-API-Key` auth path."""

    def __init__(
        self,
        config: SupabaseConfig,
        *,
        timeout: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._config = config
        self._client = httpx.AsyncClient(
            base_url=config.url,
            timeout=timeout,
            transport=transport,
            headers={"apikey": config.service_role_key},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def authorize_url(self, *, redirect_to: str, code_challenge: str, state: str) -> str:
        query = urlencode(
            {
                "provider": "github",
                "redirect_to": redirect_to,
                "code_challenge": code_challenge,
                "code_challenge_method": "s256",
                "state": state,
            }
        )
        return f"{self._config.url}/auth/v1/authorize?{query}"

    async def exchange_code(self, *, auth_code: str, code_verifier: str) -> AuthSession:
        response = await self._client.post(
            "/auth/v1/token",
            params={"grant_type": "pkce"},
            json={"auth_code": auth_code, "code_verifier": code_verifier},
        )
        if response.status_code >= 400:
            raise SupabaseAuthError("GitHub sign-in failed while exchanging the authorization code")
        return _session_from_token_payload(response.json())

    async def refresh(self, *, refresh_token: str) -> AuthSession:
        response = await self._client.post(
            "/auth/v1/token",
            params={"grant_type": "refresh_token"},
            json={"refresh_token": refresh_token},
        )
        if response.status_code >= 400:
            raise SupabaseAuthError("session refresh failed")
        return _session_from_token_payload(response.json())

    async def get_user(self, *, access_token: str) -> dict[str, Any]:
        response = await self._client.get(
            "/auth/v1/user",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if response.status_code >= 400:
            raise SupabaseAuthError("session is invalid or expired")
        payload = response.json()
        if not isinstance(payload, dict):
            raise SupabaseAuthError("Supabase user endpoint returned an unexpected payload")
        return payload

    async def logout(self, *, access_token: str) -> None:
        """Best-effort upstream revoke; callers clear local cookies regardless."""
        with suppress(httpx.HTTPError):
            await self._client.post(
                "/auth/v1/logout",
                params={"scope": "global"},
                headers={"Authorization": f"Bearer {access_token}"},
            )


class SupabaseAPIKeyAuthenticator:
    def __init__(self, client: SupabaseClient, *, required: bool = True):
        self.required = required
        self._client = client

    @property
    def enabled(self) -> bool:
        return self.required

    async def authenticate(
        self,
        presented_key: str | None,
        *,
        required_scope: str = VALUATION_SCOPE,
        now: datetime | None = None,
    ) -> AuthenticatedPrincipal | None:
        if not self.required:
            return None

        prefix, stripped = APIKeyAuthenticator.parse_presented_key(presented_key)
        record = await self._client.get_api_key_by_prefix(prefix)
        presented_hash = APIKeyAuthenticator.hash_secret(stripped)
        if record is None or not hmac.compare_digest(
            presented_hash, str(record.get("secret_hash", ""))
        ):
            raise AuthFailure(AuthFailureReason.INVALID)
        if bool(record.get("revoked")):
            raise AuthFailure(AuthFailureReason.REVOKED)

        current_time = now or datetime.now(UTC)
        expires_at = _parse_datetime(record.get("expires_at"))
        if expires_at is not None and expires_at <= current_time:
            raise AuthFailure(AuthFailureReason.EXPIRED)

        raw_scopes = record.get("scopes") or []
        if not isinstance(raw_scopes, list):
            raise SupabaseError("Supabase API-key scopes field was not a list")
        scopes = frozenset(str(scope) for scope in raw_scopes)
        if required_scope not in scopes:
            raise AuthFailure(AuthFailureReason.INSUFFICIENT_SCOPE)

        key_id = str(record["id"])
        await self._client.mark_key_used(key_id)
        return AuthenticatedPrincipal(
            key_id=key_id,
            customer_id=str(record["customer_id"]),
            prefix=str(record["prefix"]),
            scopes=scopes,
            daily_quota=int(record.get("daily_quota") or 100),
        )


class SupabaseDailyQuotaLimiter:
    def __init__(self, client: SupabaseClient, *, default_limit: int = 100):
        self._client = client
        self._default_limit = default_limit

    async def check_and_increment(
        self,
        *,
        identity: str = "anonymous",
        limit: int | None = None,
    ) -> RateLimitResult:
        now = datetime.now(UTC)
        return await self._client.consume_daily_quota(
            subject_id=identity,
            limit=limit or self._default_limit,
            window=now.date().isoformat(),
        )


class SupabaseUsageMeter:
    def __init__(self, client: SupabaseClient):
        self._client = client

    async def record(
        self,
        *,
        request_id: str,
        principal: AuthenticatedPrincipal | None,
        method: str,
        path: str,
        status_code: int,
        ticker: str | None,
        quota_consumed: bool,
        rate_limited: bool,
    ) -> None:
        await self._client.record_usage_event(
            event={
                "request_id": request_id,
                "customer_id": principal.customer_id if principal else None,
                "api_key_id": principal.key_id if principal else None,
                "method": method,
                "path": path,
                "ticker": ticker,
                "status_code": status_code,
                "quota_consumed": quota_consumed,
                "rate_limited": rate_limited,
                "recorded_at": datetime.now(UTC).isoformat(),
            }
        )
