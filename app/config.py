"""Environment settings. Construct Settings in bootstrap, never during import."""

from __future__ import annotations

import json
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit, urlunsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
        hide_input_in_errors=True,
    )

    telegram_bot_token: SecretStr
    allowed_user_ids: Annotated[list[int], NoDecode] = Field(
        validation_alias=AliasChoices("ALLOWED_USER_IDS", "TELEGRAM_ALLOWED_USER_IDS")
    )
    telegram_admin_id: int | None = Field(default=None, gt=0)
    bot_mode: Literal["polling", "webhook"] = "polling"
    webhook_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "WEBHOOK_BASE_URL", "TELEGRAM_WEBHOOK_URL", "WEBHOOK_URL"
        ),
    )
    telegram_webhook_secret: SecretStr | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "TELEGRAM_WEBHOOK_SECRET", "TELEGRAM_WEBHOOK_SECRET_TOKEN", "WEBHOOK_SECRET"
        ),
    )
    webhook_path: str = "/telegram/webhook"
    supabase_url: str
    supabase_secret_key: SecretStr = Field(
        validation_alias=AliasChoices(
            "SUPABASE_SECRET_KEY", "SUPABASE_SERVICE_ROLE_KEY"
        )
    )
    supabase_key: SecretStr | None = None
    google_client_id: str
    google_client_secret: SecretStr
    google_refresh_token: SecretStr
    google_calendar_id: str = "primary"
    timezone: str = "Asia/Seoul"
    default_event_duration_minutes: int = Field(default=60, gt=0, le=10080)
    dialog_ttl_seconds: int = Field(default=1800, ge=30, le=86400)
    app_env: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    host: str = "0.0.0.0"
    port: int = Field(default=8080, ge=1, le=65535)

    @field_validator(
        "telegram_bot_token",
        "supabase_secret_key",
        "google_client_secret",
        "google_refresh_token",
    )
    @classmethod
    def nonblank_secret(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("credential must not be blank")
        return value

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def parse_allowlist(cls, value: object) -> object:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    value = json.loads(stripped)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        "allowed user IDs must be a JSON array or comma-separated integers"
                    ) from exc
            else:
                value = [part.strip() for part in stripped.split(",") if part.strip()]
        if not isinstance(value, (list, tuple, set)) or not value:
            raise ValueError("at least one allowed Telegram user ID is required")
        if any(
            isinstance(part, bool) or not isinstance(part, (str, int)) for part in value
        ):
            raise ValueError("allowed Telegram user IDs must be positive integers")
        try:
            parsed = [int(part) for part in value]
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "allowed Telegram user IDs must be positive integers"
            ) from exc
        if any(part <= 0 for part in parsed):
            raise ValueError("allowed Telegram user IDs must be positive integers")
        return list(dict.fromkeys(parsed))

    @field_validator("supabase_url")
    @classmethod
    def normalize_supabase_url(cls, value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "SUPABASE_URL must be an HTTP(S) project URL without credentials, query, or fragment"
            )
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
        }:
            raise ValueError("remote SUPABASE_URL must use HTTPS")
        path = parsed.path.rstrip("/")
        path = path.removesuffix("/rest/v1")
        if path:
            raise ValueError(
                "SUPABASE_URL must point to the project root, optionally ending in /rest/v1"
            )
        return urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("TIMEZONE must be an IANA timezone") from exc
        return value

    @field_validator("google_calendar_id", "google_client_id")
    @classmethod
    def nonblank_calendar(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Google client/calendar ID must not be blank")
        return value.strip()

    @field_validator("webhook_path")
    @classmethod
    def valid_webhook_path(cls, value: str) -> str:
        if not re.fullmatch(r"/[A-Za-z0-9_/-]+", value) or "//" in value:
            raise ValueError("WEBHOOK_PATH must be an absolute URL path")
        return value

    @model_validator(mode="after")
    def runtime_requirements(self) -> Settings:
        if self.telegram_admin_id is None:
            self.telegram_admin_id = self.allowed_user_ids[0]
        elif self.telegram_admin_id not in self.allowed_user_ids:
            raise ValueError("TELEGRAM_ADMIN_ID must be included in ALLOWED_USER_IDS")
        if self.bot_mode == "webhook":
            if not self.webhook_base_url:
                raise ValueError("WEBHOOK_BASE_URL is required in webhook mode")
            parsed = urlsplit(self.webhook_base_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError(
                    "WEBHOOK_BASE_URL must use HTTPS without credentials, query, or fragment"
                )
            if self.telegram_webhook_secret is None:
                raise ValueError("TELEGRAM_WEBHOOK_SECRET is required in webhook mode")
            if not re.fullmatch(
                r"[A-Za-z0-9_-]{1,256}", self.telegram_webhook_secret.get_secret_value()
            ):
                raise ValueError(
                    "TELEGRAM_WEBHOOK_SECRET must contain 1-256 letters, digits, underscores, or hyphens"
                )
        return self

    @property
    def telegram_mode(self) -> str:
        return self.bot_mode

    @property
    def telegram_allowed_user_ids(self) -> list[int]:
        return self.allowed_user_ids

    @property
    def supabase_service_role_key(self) -> SecretStr:
        return self.supabase_secret_key

    @property
    def telegram_webhook_url(self) -> str | None:
        return (
            self.webhook_base_url.rstrip("/") + self.webhook_path
            if self.webhook_base_url
            else None
        )

    @property
    def zoneinfo(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def get_settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]
