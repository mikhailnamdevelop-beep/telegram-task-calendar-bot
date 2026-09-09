"""Configuration tests use synthetic placeholders only and never read .env."""

import pytest
from pydantic import ValidationError

from app.config import Settings


@pytest.fixture
def settings_values():
    return {
        "telegram_bot_token": "000000:placeholder-only",
        "allowed_user_ids": [12345, 67890],
        "supabase_url": "https://placeholder-project.supabase.co",
        "supabase_secret_key": "placeholder-secret-key",
        "google_client_id": "placeholder.apps.googleusercontent.com",
        "google_client_secret": "placeholder-client-secret",
        "google_refresh_token": "placeholder-refresh-token",
        "_env_file": None,
    }


def test_required_configuration_has_safe_public_defaults(settings_values):
    settings = Settings(**settings_values)
    assert settings.telegram_admin_id == 12345
    assert settings.bot_mode == "polling"
    assert settings.port == 8080
    assert settings.timezone == "Asia/Seoul"
    assert settings.google_calendar_id == "primary"
    assert settings.zoneinfo.key == "Asia/Seoul"
    assert settings.telegram_webhook_url is None
    assert "placeholder-refresh-token" not in repr(settings)
    assert "placeholder-secret-key" not in repr(settings)


@pytest.mark.parametrize(
    "value", ["12345,67890", "[12345, 67890]", [12345, 67890, 12345]]
)
def test_allowlist_formats_and_deduplication(settings_values, value):
    settings_values["allowed_user_ids"] = value
    assert Settings(**settings_values).allowed_user_ids == [12345, 67890]


@pytest.mark.parametrize("value", ["", [], "one,two", [0], [-1], [True], "[oops]"])
def test_invalid_allowlist_fails_closed(settings_values, value):
    settings_values["allowed_user_ids"] = value
    with pytest.raises(ValidationError):
        Settings(**settings_values)


@pytest.mark.parametrize(
    "url, expected",
    [
        (" https://placeholder.supabase.co/ ", "https://placeholder.supabase.co"),
        ("https://placeholder.supabase.co/rest/v1/", "https://placeholder.supabase.co"),
        ("http://127.0.0.1:54321/rest/v1", "http://127.0.0.1:54321"),
    ],
)
def test_supabase_url_normalization(settings_values, url, expected):
    settings_values["supabase_url"] = url
    assert Settings(**settings_values).supabase_url == expected


@pytest.mark.parametrize(
    "url",
    [
        "placeholder.supabase.co",
        "http://placeholder.supabase.co",
        "https://user:password@placeholder.supabase.co",
        "https://placeholder.supabase.co/rest/v1/items",
        "https://placeholder.supabase.co/?key=bad",
        "https://placeholder.supabase.co/#bad",
        "file:///tmp/db",
    ],
)
def test_bad_supabase_url_is_rejected(settings_values, url):
    settings_values["supabase_url"] = url
    with pytest.raises(ValidationError):
        Settings(**settings_values)


def test_explicit_admin_must_be_allowed(settings_values):
    settings_values["telegram_admin_id"] = 11111
    with pytest.raises(ValidationError, match="included"):
        Settings(**settings_values)


def test_invalid_timezone(settings_values):
    settings_values["timezone"] = "Mars/Olympus"
    with pytest.raises(ValidationError, match="IANA"):
        Settings(**settings_values)


def test_webhook_is_conditional(settings_values):
    settings_values["bot_mode"] = "webhook"
    with pytest.raises(ValidationError, match="WEBHOOK_BASE_URL"):
        Settings(**settings_values)
    settings_values["webhook_base_url"] = "https://placeholder.example"
    with pytest.raises(ValidationError, match="TELEGRAM_WEBHOOK_SECRET"):
        Settings(**settings_values)
    settings_values["telegram_webhook_secret"] = "placeholder_webhook-secret"
    settings = Settings(**settings_values)
    assert (
        settings.telegram_webhook_url == "https://placeholder.example/telegram/webhook"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://placeholder.example",
        "https://user:pass@placeholder.example",
        "https://placeholder.example?token=x",
    ],
)
def test_webhook_requires_clean_https(settings_values, url):
    settings_values.update(
        bot_mode="webhook", webhook_base_url=url, telegram_webhook_secret="placeholder"
    )
    with pytest.raises(ValidationError):
        Settings(**settings_values)


@pytest.mark.parametrize("secret", ["", "contains space", "кириллица", "x" * 257])
def test_webhook_secret_format(settings_values, secret):
    settings_values.update(
        bot_mode="webhook",
        webhook_base_url="https://placeholder.example",
        telegram_webhook_secret=secret,
    )
    with pytest.raises(ValidationError):
        Settings(**settings_values)


def test_environment_variable_names(settings_values, monkeypatch):
    mapping = {
        "TELEGRAM_BOT_TOKEN": settings_values["telegram_bot_token"],
        "ALLOWED_USER_IDS": "12345,67890",
        "SUPABASE_URL": settings_values["supabase_url"],
        "SUPABASE_SECRET_KEY": settings_values["supabase_secret_key"],
        "GOOGLE_CLIENT_ID": settings_values["google_client_id"],
        "GOOGLE_CLIENT_SECRET": settings_values["google_client_secret"],
        "GOOGLE_REFRESH_TOKEN": settings_values["google_refresh_token"],
        "BOT_MODE": "polling",
    }
    for name, value in mapping.items():
        monkeypatch.setenv(name, value)
    settings = Settings(_env_file=None)
    assert settings.supabase_secret_key.get_secret_value() == "placeholder-secret-key"
    assert settings.allowed_user_ids == [12345, 67890]


def test_legacy_supabase_key_alias(settings_values):
    settings_values["SUPABASE_SERVICE_ROLE_KEY"] = settings_values.pop(
        "supabase_secret_key"
    )
    assert (
        Settings(**settings_values).supabase_service_role_key.get_secret_value()
        == "placeholder-secret-key"
    )


@pytest.mark.parametrize(
    "field",
    [
        "telegram_bot_token",
        "supabase_secret_key",
        "google_client_secret",
        "google_refresh_token",
    ],
)
def test_empty_credentials_are_rejected_without_printing_inputs(settings_values, field):
    settings_values[field] = " "
    with pytest.raises(ValidationError) as caught:
        Settings(**settings_values)
    assert "placeholder-refresh-token" not in str(caught.value)
