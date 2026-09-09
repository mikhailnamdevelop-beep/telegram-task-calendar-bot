from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot
from pydantic import SecretStr

from app import main
from app.config import Settings
from app.parser import CommandParser


def settings(*, mode: str = "polling") -> Settings:
    values: dict[str, Any] = {
        "telegram_bot_token": "placeholder",
        "allowed_user_ids": [7],
        "supabase_url": "https://project.example.com",
        "supabase_secret_key": "placeholder",
        "google_client_id": "client-id",
        "google_client_secret": "placeholder",
        "google_refresh_token": "placeholder",
        "bot_mode": mode,
    }
    if mode == "webhook":
        values.update(
            webhook_base_url="https://bot.example.com",
            telegram_webhook_secret="placeholder_secret",
        )
    return Settings(**values)


@pytest.mark.asyncio
async def test_health() -> None:
    response = await main.health(None)  # type: ignore[arg-type]
    assert response.status == 200
    assert response.text == '{"status": "ok"}'


def test_create_app_and_web_app_routes() -> None:
    configured = settings(mode="webhook")
    dispatcher = main.create_app(SimpleNamespace(), configured, CommandParser())
    bot = Bot("123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijk")
    web_app = main.build_web_app(bot, dispatcher, configured)
    paths = {resource.canonical for resource in web_app.router.resources()}
    assert "/health" in paths
    assert "/telegram/webhook" in paths


def test_create_service_wires_secret_values(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, Any] = {}

    class Repository:
        @classmethod
        def from_credentials(cls, url: str, key: str) -> Any:
            calls["repository"] = (url, key)
            return "repository"

    class Calendar:
        @classmethod
        def from_refresh_token(cls, **kwargs: Any) -> Any:
            calls["calendar"] = kwargs
            return "calendar"

    class Service:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            calls["service"] = (args, kwargs)

    monkeypatch.setattr(main, "SupabaseAdapter", Repository)
    monkeypatch.setattr(main, "GoogleCalendarAdapter", Calendar)
    monkeypatch.setattr(main, "ItemService", Service)

    main._create_service(settings())

    assert calls["repository"][0] == "https://project.example.com"
    assert calls["calendar"]["calendar_id"] == "primary"
    assert calls["service"][1]["dialog_ttl_seconds"] == 1800


class AsyncBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def set_my_commands(self, commands: Any) -> None:
        self.calls.append(("commands", commands))

    async def delete_webhook(self, **kwargs: Any) -> None:
        self.calls.append(("delete_webhook", kwargs))

    async def set_webhook(self, url: str, **kwargs: Any) -> None:
        self.calls.append(("set_webhook", (url, kwargs)))


class AsyncDispatcher:
    def resolve_used_update_types(self) -> list[str]:
        return ["message"]

    async def start_polling(self, bot: Any, **kwargs: Any) -> None:
        self.started = (bot, kwargs)


@pytest.mark.asyncio
async def test_polling_and_webhook_registration() -> None:
    bot = AsyncBot()
    dispatcher = AsyncDispatcher()
    await main._start_polling(bot, dispatcher)  # type: ignore[arg-type]
    assert [call[0] for call in bot.calls] == ["commands", "delete_webhook"]
    assert dispatcher.started[1]["allowed_updates"] == ["message"]

    bot.calls.clear()
    configured = settings(mode="webhook")
    await main._set_webhook(bot, dispatcher, configured)  # type: ignore[arg-type]
    assert bot.calls[-1][1][0] == "https://bot.example.com/telegram/webhook"


@pytest.mark.asyncio
async def test_webhook_registration_rejects_missing_url() -> None:
    configured = settings()
    configured.bot_mode = "webhook"
    with pytest.raises(RuntimeError, match="WEBHOOK_BASE_URL"):
        await main._set_webhook(  # type: ignore[arg-type]
            AsyncBot(), AsyncDispatcher(), configured
        )


def test_runtime_settings_keep_secrets_wrapped() -> None:
    configured = settings()
    assert isinstance(configured.telegram_bot_token, SecretStr)
    assert "placeholder" not in repr(configured)
