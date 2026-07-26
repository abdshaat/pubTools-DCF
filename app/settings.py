"""One typed, validated source for every environment-driven setting.

Phase 11 Slice 1. Before this module the app read `os.environ` from eight
places, several of them per request, with no validation: a misspelled variable
name or a `PROVIDER_TIMEOUT_SECONDS=six` was discovered by a customer request,
not at boot. `Settings.from_env()` reads everything once, reports *all* invalid
values together with the variable names, and is stored on `app.state.settings`.

Composition, not replacement: the feature-detecting configs that already exist
(`SupabaseConfig`, `RedisConfig`, `FinnhubConfig`) keep their auto-enable
behavior — absent credentials still mean "feature off" — and are carried here so
one object answers "how is this deployment configured".

Two deliberate exceptions, so this docstring stays true:

- `app/auth.py` still reads `API_KEY_HASH_PEPPER` at hash/verify time. The
  peppered-hash helpers are static and shared with `scripts/create_api_key.py`,
  and a rotation must take effect for the very next verification, so the value
  is read where it is used. `Settings` still validates and reports its presence.
- `app/accounts.py::public_base_url()` remains as a fallback for direct callers;
  the request path passes `Settings.public_base_url` explicitly.
"""

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlsplit

from .providers.finnhub import FinnhubConfig
from .raw_store import DEFAULT_MAX_CAPTURES_PER_ENDPOINT, DEFAULT_RETENTION_DAYS
from .readiness import DEFAULT_READINESS_CACHE_SECONDS
from .redis_cache import RedisConfig
from .supabase import SupabaseConfig

DEFAULT_PUBLIC_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_DAILY_RATE_LIMIT = 100
DEFAULT_FUNDAMENTALS_TTL_SECONDS = 4 * 3600.0
DEFAULT_PROFILE_TTL_SECONDS = 24 * 3600.0
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 6.0
DEFAULT_PROVIDER_MAX_RETRIES = 2
DEFAULT_PROVIDER_CONCURRENCY = 3
MINIMUM_CRON_SECRET_LENGTH = 16
MINIMUM_METRICS_TOKEN_LENGTH = 16
# Vercel puts exactly one proxy in front of the function; off-platform runs
# talk to the client directly and must believe no forwarding header.
DEFAULT_TRUSTED_PROXY_HOPS_ON_VERCEL = 1

_LOG_FORMATS = ("json", "text")


class SettingsError(Exception):
    """Configuration is invalid. Raised at startup, never mid-request."""


@dataclass(frozen=True)
class Settings:
    """Everything this deployment's behavior depends on, resolved and checked."""

    environment: str
    on_vercel: bool
    public_base_url: str
    cron_secret: str | None
    metrics_token: str | None
    api_key_hash_pepper: str | None
    daily_rate_limit: int
    fundamentals_ttl_seconds: float
    profile_ttl_seconds: float
    provider_timeout_seconds: float
    provider_max_retries: int
    provider_concurrency: int
    raw_capture_retention_days: int
    raw_capture_max_per_endpoint: int
    log_level: str
    log_format: str
    readiness_cache_seconds: float
    trusted_proxy_hops: int
    supabase: SupabaseConfig | None
    redis: RedisConfig | None
    finnhub: FinnhubConfig | None

    @classmethod
    def from_env(cls) -> "Settings":
        """Resolve and validate. Raises SettingsError listing every problem."""
        env = os.environ
        errors: list[str] = []
        on_vercel = bool(env.get("VERCEL"))
        # Vercel sets VERCEL_ENV to production/preview/development. Off-platform
        # runs are "local" unless ENVIRONMENT says otherwise, so logs and
        # readiness output can tell a laptop from production at a glance.
        environment = (
            env.get("ENVIRONMENT")
            or env.get("VERCEL_ENV")
            or ("production" if on_vercel else "local")
        )

        public_base_url = env.get("PUBLIC_BASE_URL", DEFAULT_PUBLIC_BASE_URL).strip().rstrip("/")
        if not _is_absolute_http_url(public_base_url):
            errors.append(
                "PUBLIC_BASE_URL must be an absolute http(s) URL "
                f"(got {public_base_url!r}); sign-in redirects are built from it"
            )

        cron_secret = _optional(env.get("CRON_SECRET"))
        if cron_secret is not None and len(cron_secret) < MINIMUM_CRON_SECRET_LENGTH:
            errors.append(
                f"CRON_SECRET must be at least {MINIMUM_CRON_SECRET_LENGTH} characters; "
                "it is the only thing protecting the internal refresh endpoint"
            )

        metrics_token = _optional(env.get("METRICS_TOKEN"))
        if metrics_token is not None and len(metrics_token) < MINIMUM_METRICS_TOKEN_LENGTH:
            errors.append(
                f"METRICS_TOKEN must be at least {MINIMUM_METRICS_TOKEN_LENGTH} characters; "
                "it is the only thing guarding the metrics endpoint"
            )

        log_level = env.get("LOG_LEVEL", "INFO").strip().upper()
        if logging.getLevelNamesMapping().get(log_level) is None:
            errors.append(f"LOG_LEVEL must be a Python logging level name (got {log_level!r})")
        log_format = env.get("LOG_FORMAT", "json" if on_vercel else "text").strip().lower()
        if log_format not in _LOG_FORMATS:
            errors.append(f"LOG_FORMAT must be one of {_LOG_FORMATS} (got {log_format!r})")

        settings = cls(
            environment=environment,
            on_vercel=on_vercel,
            public_base_url=public_base_url,
            cron_secret=cron_secret,
            metrics_token=metrics_token,
            # Never length-checked: rotating the pepper invalidates every stored
            # hash, so rejecting a short one at boot would turn a weak setting
            # into an outage with no safe fix. Strength is a key-issuance
            # concern, not a startup gate.
            api_key_hash_pepper=_optional(env.get("API_KEY_HASH_PEPPER")),
            daily_rate_limit=_int(env, "DAILY_RATE_LIMIT", DEFAULT_DAILY_RATE_LIMIT, 1, errors),
            fundamentals_ttl_seconds=_float(
                env, "FUNDAMENTALS_TTL_SECONDS", DEFAULT_FUNDAMENTALS_TTL_SECONDS, 1.0, errors
            ),
            profile_ttl_seconds=_float(
                env, "PROFILE_TTL_SECONDS", DEFAULT_PROFILE_TTL_SECONDS, 1.0, errors
            ),
            provider_timeout_seconds=_float(
                env, "PROVIDER_TIMEOUT_SECONDS", DEFAULT_PROVIDER_TIMEOUT_SECONDS, 0.1, errors
            ),
            provider_max_retries=_int(
                env, "PROVIDER_MAX_RETRIES", DEFAULT_PROVIDER_MAX_RETRIES, 0, errors
            ),
            provider_concurrency=_int(
                env, "PROVIDER_CONCURRENCY", DEFAULT_PROVIDER_CONCURRENCY, 1, errors
            ),
            raw_capture_retention_days=_int(
                env, "RAW_CAPTURE_RETENTION_DAYS", DEFAULT_RETENTION_DAYS, 1, errors
            ),
            raw_capture_max_per_endpoint=_int(
                env,
                "RAW_CAPTURE_MAX_PER_ENDPOINT",
                DEFAULT_MAX_CAPTURES_PER_ENDPOINT,
                1,
                errors,
            ),
            log_level=log_level,
            log_format=log_format,
            readiness_cache_seconds=_float(
                env, "READINESS_CACHE_SECONDS", DEFAULT_READINESS_CACHE_SECONDS, 0.0, errors
            ),
            trusted_proxy_hops=_int(
                env,
                "TRUSTED_PROXY_HOPS",
                DEFAULT_TRUSTED_PROXY_HOPS_ON_VERCEL if on_vercel else 0,
                0,
                errors,
            ),
            supabase=SupabaseConfig.from_env(),
            redis=RedisConfig.from_env(),
            finnhub=FinnhubConfig.from_env(),
        )
        if errors:
            raise SettingsError("invalid configuration: " + "; ".join(errors))
        return settings

    def with_overrides(self, **overrides: Any) -> "Settings":
        """A copy with fields replaced — for tests and explicit call-site
        overrides (`create_app(daily_rate_limit=...)`), never for env reads."""
        return replace(self, **overrides)

    def redacted(self) -> dict[str, Any]:
        """Loggable view: values that are secrets collapse to set/unset."""
        return {
            "environment": self.environment,
            "on_vercel": self.on_vercel,
            "public_base_url": self.public_base_url,
            "cron_secret": _presence(self.cron_secret),
            "metrics_token": _presence(self.metrics_token),
            "api_key_hash_pepper": _presence(self.api_key_hash_pepper),
            "daily_rate_limit": self.daily_rate_limit,
            "fundamentals_ttl_seconds": self.fundamentals_ttl_seconds,
            "profile_ttl_seconds": self.profile_ttl_seconds,
            "provider_timeout_seconds": self.provider_timeout_seconds,
            "provider_max_retries": self.provider_max_retries,
            "provider_concurrency": self.provider_concurrency,
            "raw_capture_retention_days": self.raw_capture_retention_days,
            "raw_capture_max_per_endpoint": self.raw_capture_max_per_endpoint,
            "log_level": self.log_level,
            "log_format": self.log_format,
            "readiness_cache_seconds": self.readiness_cache_seconds,
            "trusted_proxy_hops": self.trusted_proxy_hops,
            # Features report enabled/disabled only: the configs hold service
            # URLs and service-role keys, and this dict exists to be logged.
            "supabase": _presence(self.supabase),
            "redis": _presence(self.redis),
            "finnhub": _presence(self.finnhub),
        }


def _presence(value: Any) -> str:
    return "set" if value else "unset"


def _optional(value: str | None) -> str | None:
    stripped = (value or "").strip()
    return stripped or None


def _is_absolute_http_url(value: str) -> bool:
    parts = urlsplit(value)
    return parts.scheme in ("http", "https") and bool(parts.netloc)


def _int(env: Mapping[str, str], name: str, default: int, minimum: int, errors: list[str]) -> int:
    raw = _optional(env.get(name))
    if raw is None:
        return default
    try:
        value = int(raw)
    except ValueError:
        errors.append(f"{name} must be an integer (got {raw!r})")
        return default
    if value < minimum:
        errors.append(f"{name} must be at least {minimum} (got {value})")
        return default
    return value


def _float(
    env: Mapping[str, str], name: str, default: float, minimum: float, errors: list[str]
) -> float:
    raw = _optional(env.get(name))
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        errors.append(f"{name} must be a number (got {raw!r})")
        return default
    if value < minimum:
        errors.append(f"{name} must be at least {minimum} (got {value})")
        return default
    return value
