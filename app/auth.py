"""API key authentication primitives.

Keys are stored as versioned hashes of the full presented secret. A short public
prefix identifies the candidate record so the service never needs to compare a
caller secret against every stored key.
"""

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

VALUATION_SCOPE = "valuation:read"
LEGACY_SHA256_PREFIX = "sha256"
PEPPERED_SHA256_PREFIX = "hmac-sha256-v1"


class AuthFailureReason(StrEnum):
    MISSING = "missing"
    MALFORMED = "malformed"
    INVALID = "invalid"
    REVOKED = "revoked"
    EXPIRED = "expired"
    INSUFFICIENT_SCOPE = "insufficient_scope"


class AuthFailure(Exception):
    def __init__(self, reason: AuthFailureReason):
        self.reason = reason
        super().__init__(reason.value)


@dataclass(frozen=True)
class APIKeyRecord:
    key_id: str
    customer_id: str
    prefix: str
    secret_hash: str
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({VALUATION_SCOPE}))
    revoked: bool = False
    expires_at: datetime | None = None
    daily_quota: int | None = None


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    key_id: str
    customer_id: str
    prefix: str
    scopes: frozenset[str]
    daily_quota: int | None = None


class APIKeyAuthenticator:
    def __init__(
        self,
        records: list[APIKeyRecord] | None = None,
        *,
        required: bool = False,
    ):
        self.required = required
        self._records = {record.prefix: record for record in records or []}

    @staticmethod
    def hash_secret(secret: str) -> str:
        encoded_secret = secret.encode("utf-8")
        pepper = os.environ.get("API_KEY_HASH_PEPPER")
        if pepper:
            digest = hmac.new(pepper.encode("utf-8"), encoded_secret, hashlib.sha256).hexdigest()
            return f"{PEPPERED_SHA256_PREFIX}:{digest}"
        return f"{LEGACY_SHA256_PREFIX}:{hashlib.sha256(encoded_secret).hexdigest()}"

    @staticmethod
    def verify_secret(secret: str, stored_hash: str) -> bool:
        encoded_secret = secret.encode("utf-8")
        prefix, separator, digest = stored_hash.partition(":")
        if separator and prefix == PEPPERED_SHA256_PREFIX:
            pepper = os.environ.get("API_KEY_HASH_PEPPER")
            if not pepper:
                return False
            expected = hmac.new(pepper.encode("utf-8"), encoded_secret, hashlib.sha256).hexdigest()
        elif separator and prefix == LEGACY_SHA256_PREFIX:
            expected = hashlib.sha256(encoded_secret).hexdigest()
        else:
            # Backward compatibility for hashes created before version prefixes.
            digest = stored_hash
            expected = hashlib.sha256(encoded_secret).hexdigest()
        return hmac.compare_digest(expected, digest)

    @classmethod
    def record_from_secret(
        cls,
        *,
        key_id: str,
        customer_id: str | None = None,
        prefix: str,
        secret: str,
        scopes: set[str] | frozenset[str] | None = None,
        revoked: bool = False,
        expires_at: datetime | None = None,
        daily_quota: int | None = None,
    ) -> APIKeyRecord:
        return APIKeyRecord(
            key_id=key_id,
            customer_id=customer_id or key_id,
            prefix=prefix,
            secret_hash=cls.hash_secret(secret),
            scopes=frozenset(scopes or {VALUATION_SCOPE}),
            revoked=revoked,
            expires_at=expires_at,
            daily_quota=daily_quota,
        )

    @property
    def enabled(self) -> bool:
        return self.required

    @staticmethod
    def parse_presented_key(presented_key: str | None) -> tuple[str, str]:
        if presented_key is None or not presented_key.strip():
            raise AuthFailure(AuthFailureReason.MISSING)

        stripped = presented_key.strip()
        parts = stripped.split("_", 2)
        if len(parts) != 3 or parts[0] != "dcf" or not parts[1] or not parts[2]:
            raise AuthFailure(AuthFailureReason.MALFORMED)
        return parts[1], stripped

    def authenticate(
        self,
        presented_key: str | None,
        *,
        required_scope: str = VALUATION_SCOPE,
        now: datetime | None = None,
    ) -> AuthenticatedPrincipal | None:
        if not self.required:
            return None

        prefix, stripped = self.parse_presented_key(presented_key)
        record = self._records.get(prefix)
        if record is None or not self.verify_secret(stripped, record.secret_hash):
            raise AuthFailure(AuthFailureReason.INVALID)
        if record.revoked:
            raise AuthFailure(AuthFailureReason.REVOKED)
        current_time = now or datetime.now(UTC)
        if record.expires_at is not None and record.expires_at <= current_time:
            raise AuthFailure(AuthFailureReason.EXPIRED)
        if required_scope not in record.scopes:
            raise AuthFailure(AuthFailureReason.INSUFFICIENT_SCOPE)
        return AuthenticatedPrincipal(
            key_id=record.key_id,
            customer_id=record.customer_id,
            prefix=record.prefix,
            scopes=record.scopes,
            daily_quota=record.daily_quota,
        )
