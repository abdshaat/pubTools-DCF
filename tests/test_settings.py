"""Tests for typed configuration (Phase 11 Slice 1).

The point of this layer is that a bad value stops the app at boot with the
variable named, instead of surfacing as a strange customer-facing failure much
later — and that feature auto-enable behavior is unchanged by centralizing the
reads.
"""

import pytest

from app.api import create_app
from app.auth import APIKeyAuthenticator
from app.providers.fmp import FMPClient
from app.settings import (
    DEFAULT_DAILY_RATE_LIMIT,
    DEFAULT_PUBLIC_BASE_URL,
    MINIMUM_CRON_SECRET_LENGTH,
    Settings,
    SettingsError,
)
from tests.test_data_layer import fixture_transport

VALID_QUERY = (
    "wacc=0.09&terminal_growth=0.025&ebit_margin=0.30&revenue_growth=0.05&projection_years=5"
)


def test_defaults_are_usable_with_an_empty_environment():
    settings = Settings.from_env()
    assert settings.environment == "local"
    assert settings.on_vercel is False
    assert settings.public_base_url == DEFAULT_PUBLIC_BASE_URL
    assert settings.daily_rate_limit == DEFAULT_DAILY_RATE_LIMIT
    assert settings.cron_secret is None
    assert settings.log_format == "text"


def test_vercel_environment_is_detected(monkeypatch):
    monkeypatch.setenv("VERCEL", "1")
    monkeypatch.setenv("VERCEL_ENV", "preview")
    settings = Settings.from_env()
    assert settings.on_vercel is True
    assert settings.environment == "preview"
    # Structured logs default to machine-readable on the platform, human
    # -readable on a laptop.
    assert settings.log_format == "json"


def test_values_are_read_and_typed(monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://ashaat.dev/")
    monkeypatch.setenv("DAILY_RATE_LIMIT", "250")
    monkeypatch.setenv("FUNDAMENTALS_TTL_SECONDS", "60")
    monkeypatch.setenv("PROVIDER_TIMEOUT_SECONDS", "2.5")
    monkeypatch.setenv("PROVIDER_MAX_RETRIES", "0")
    monkeypatch.setenv("RAW_CAPTURE_RETENTION_DAYS", "7")

    settings = Settings.from_env()
    assert settings.public_base_url == "https://ashaat.dev"  # trailing slash normalized
    assert settings.daily_rate_limit == 250
    assert settings.fundamentals_ttl_seconds == 60.0
    assert settings.provider_timeout_seconds == 2.5
    assert settings.provider_max_retries == 0
    assert settings.raw_capture_retention_days == 7


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        ("PUBLIC_BASE_URL", "ashaat.dev"),  # not absolute
        ("PUBLIC_BASE_URL", "ftp://ashaat.dev"),  # not http(s)
        ("DAILY_RATE_LIMIT", "many"),
        ("DAILY_RATE_LIMIT", "0"),
        ("FUNDAMENTALS_TTL_SECONDS", "-1"),
        ("PROVIDER_TIMEOUT_SECONDS", "six"),
        ("PROVIDER_CONCURRENCY", "0"),
        ("RAW_CAPTURE_MAX_PER_ENDPOINT", "0"),
        ("LOG_LEVEL", "CHATTY"),
        ("LOG_FORMAT", "yaml"),
    ],
)
def test_invalid_values_fail_at_startup_naming_the_variable(monkeypatch, variable, value):
    monkeypatch.setenv(variable, value)
    with pytest.raises(SettingsError) as failure:
        Settings.from_env()
    assert variable in str(failure.value)


def test_a_short_cron_secret_is_refused(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "x" * (MINIMUM_CRON_SECRET_LENGTH - 1))
    with pytest.raises(SettingsError, match="CRON_SECRET"):
        Settings.from_env()

    monkeypatch.setenv("CRON_SECRET", "x" * MINIMUM_CRON_SECRET_LENGTH)
    assert Settings.from_env().cron_secret == "x" * MINIMUM_CRON_SECRET_LENGTH


def test_every_invalid_value_is_reported_at_once(monkeypatch):
    """One boot, one complete list — not a fix-one-rerun-discover-another loop."""
    monkeypatch.setenv("DAILY_RATE_LIMIT", "lots")
    monkeypatch.setenv("PROVIDER_CONCURRENCY", "-3")
    monkeypatch.setenv("LOG_FORMAT", "xml")

    with pytest.raises(SettingsError) as failure:
        Settings.from_env()
    message = str(failure.value)
    assert "DAILY_RATE_LIMIT" in message
    assert "PROVIDER_CONCURRENCY" in message
    assert "LOG_FORMAT" in message


def test_features_stay_off_when_their_credentials_are_absent():
    settings = Settings.from_env()
    assert settings.supabase is None
    assert settings.redis is None
    assert settings.finnhub is None


def test_features_auto_enable_exactly_as_before(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    monkeypatch.setenv("UPSTASH_REDIS_REST_URL", "https://example.upstash.io")
    monkeypatch.setenv("UPSTASH_REDIS_REST_TOKEN", "upstash-token")
    monkeypatch.setenv("FINNHUB_API_KEY", "finnhub-key")

    settings = Settings.from_env()
    assert settings.supabase is not None
    assert settings.redis is not None
    assert settings.finnhub is not None


def test_redacted_view_never_leaks_a_secret(monkeypatch):
    monkeypatch.setenv("CRON_SECRET", "correct-horse-battery-staple")
    monkeypatch.setenv("API_KEY_HASH_PEPPER", "pepper-value-not-for-logs")
    monkeypatch.setenv("SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "service-role-key")
    monkeypatch.setenv("FINNHUB_API_KEY", "finnhub-key")

    redacted = Settings.from_env().redacted()
    rendered = repr(redacted)
    for secret in (
        "correct-horse-battery-staple",
        "pepper-value-not-for-logs",
        "service-role-key",
        "finnhub-key",
        "example.supabase.co",
    ):
        assert secret not in rendered
    # Presence still has to be visible, or the view is useless for diagnosis.
    assert redacted["cron_secret"] == "set"
    assert redacted["api_key_hash_pepper"] == "set"
    assert redacted["supabase"] == "set"
    assert redacted["finnhub"] == "set"
    assert redacted["redis"] == "unset"
    assert redacted["environment"] == "local"


def test_with_overrides_does_not_mutate_the_original():
    settings = Settings.from_env()
    changed = settings.with_overrides(daily_rate_limit=7)
    assert changed.daily_rate_limit == 7
    assert settings.daily_rate_limit == DEFAULT_DAILY_RATE_LIMIT


def test_app_exposes_its_settings_and_honors_explicit_overrides(monkeypatch):
    monkeypatch.setenv("DAILY_RATE_LIMIT", "9")
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    app = create_app(fmp_client=fmp, authenticator=APIKeyAuthenticator(required=False))
    assert app.state.settings.daily_rate_limit == 9

    # An explicit argument beats the environment, which is what lets the test
    # suite and the load probe pin a value without touching os.environ.
    override = create_app(
        fmp_client=fmp,
        daily_rate_limit=4242,
        authenticator=APIKeyAuthenticator(required=False),
    )
    assert override.state.settings.daily_rate_limit == 4242


def test_configuration_drives_the_running_app(monkeypatch):
    """A setting that is read but never applied is worse than no setting."""
    monkeypatch.setenv("DAILY_RATE_LIMIT", "1")
    fmp = FMPClient(api_key="test-key", transport=fixture_transport())
    app = create_app(fmp_client=fmp, authenticator=APIKeyAuthenticator(required=False))

    from fastapi.testclient import TestClient

    with TestClient(app) as client:
        first = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")
        second = client.get(f"/v1/valuations/AAPL?{VALID_QUERY}")

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "1"
    assert second.status_code == 429


def test_provider_settings_reach_the_provider_client(monkeypatch):
    monkeypatch.setenv("PROVIDER_TIMEOUT_SECONDS", "1.5")
    monkeypatch.setenv("PROVIDER_MAX_RETRIES", "0")
    monkeypatch.setenv("PROVIDER_CONCURRENCY", "1")
    settings = Settings.from_env()
    assert (settings.provider_timeout_seconds, settings.provider_max_retries) == (1.5, 0)
    assert settings.provider_concurrency == 1
