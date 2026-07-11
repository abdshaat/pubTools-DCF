"""API key authentication primitives.

Keys are stored as SHA-256 hashes of the full presented secret. A short public
prefix identifies the candidate record so the service never needs to compare a
caller secret against every stored key.
"""

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

VALUATION_SCOPE = "valuation:read"


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
    prefix: str
    secret_hash: str
    scopes: frozenset[str] = field(default_factory=lambda: frozenset({VALUATION_SCOPE}))
    revoked: bool = False
    expires_at: datetime | None = None


@dataclass(frozen=True)
class AuthenticatedPrincipal:
    key_id: str
    prefix: str
    scopes: frozenset[str]


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
        return hashlib.sha256(secret.encode("utf-8")).hexdigest()

    @classmethod
    def record_from_secret(
        cls,
        *,
        key_id: str,
        prefix: str,
        secret: str,
        scopes: set[str] | frozenset[str] | None = None,
        revoked: bool = False,
        expires_at: datetime | None = None,
    ) -> APIKeyRecord:
        return APIKeyRecord(
            key_id=key_id,
            prefix=prefix,
            secret_hash=cls.hash_secret(secret),
            scopes=frozenset(scopes or {VALUATION_SCOPE}),
            revoked=revoked,
            expires_at=expires_at,
        )

    @property
    def enabled(self) -> bool:
        return self.required

    def authenticate(
        self,
        presented_key: str | None,
        *,
        required_scope: str = VALUATION_SCOPE,
        now: datetime | None = None,
    ) -> AuthenticatedPrincipal | None:
        if not self.required:
            return None
        if presented_key is None or not presented_key.strip():
            raise AuthFailure(AuthFailureReason.MISSING)

        parts = presented_key.strip().split("_", 2)
        if len(parts) != 3 or parts[0] != "dcf" or not parts[1] or not parts[2]:
            raise AuthFailure(AuthFailureReason.MALFORMED)

        prefix = parts[1]
        record = self._records.get(prefix)
        presented_hash = self.hash_secret(presented_key.strip())
        if record is None or not hmac.compare_digest(presented_hash, record.secret_hash):
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
            prefix=record.prefix,
            scopes=record.scopes,
        )
