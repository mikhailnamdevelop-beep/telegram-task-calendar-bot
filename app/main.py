"""Application entry point for local polling and Northflank webhooks."""

from __future__ import annotations

import logging

from aiogram import Bot, Dispatcher
from aiogram.types import BotCommand
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web

from app.adapters.google_calendar import GoogleCalendarAdapter
from app.adapters.supabase import SupabaseAdapter
from app.bot import TelegramController, create_router
from app.config import Settings
from app.parser import CommandParser
from app.services import ItemService

BOT_COMMANDS = [
    BotCommand(command="task", description="Add a task"),
    BotCommand(command="calendar", description="Propose calendar item"),
    BotCommand(command="both", description="Task and calendar proposal"),
    BotCommand(command="agenda", description="View agenda"),
    BotCommand(command="edit", description="Edit an item"),
    BotCommand(command="delete", description="Delete an item"),
    BotCommand(command="done", description="Complete an item"),
    BotCommand(command="today", description="Today's agenda"),
    BotCommand(command="tomorrow", description="Tomorrow's agenda"),
    BotCommand(command="week", description="This week's agenda"),
    BotCommand(command="cancel", description="Cancel current action"),
    BotCommand(command="help", description="Show help"),
]


def create_app(service: ItemService, settings: Settings, parser: CommandParser) -> Dispatcher:
    """Build a dispatcher from injected collaborators for tests and runtime."""
    dispatcher = Dispatcher()
    controller = TelegramController(service, parser)
    dispatcher.include_router(create_router(controller, set(settings.allowed_user_ids)))
    return dispatcher


async def health(_: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def build_web_app(bot: Bot, dispatcher: Dispatcher, settings: Settings) -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health)
    SimpleRequestHandler(
        dispatcher=dispatcher,
        bot=bot,
        secret_token=settings.telegram_webhook_secret.get_secret_value()
        if settings.telegram_webhook_secret
        else None,
    ).register(app, path=settings.webhook_path)
    setup_application(app, dispatcher, bot=bot)
    return app


def _create_service(settings: Settings) -> ItemService:
    repository = SupabaseAdapter.from_credentials(
        settings.supabase_url, settings.supabase_secret_key.get_secret_value()
    )
    calendar = GoogleCalendarAdapter.from_refresh_token(
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret.get_secret_value(),
        refresh_token=settings.google_refresh_token.get_secret_value(),
        calendar_id=settings.google_calendar_id,
        timezone_name=settings.timezone,
    )
    return ItemService(
        repository,
        calendar,
        settings.timezone,
        dialog_ttl_seconds=settings.dialog_ttl_seconds,
    )


async def _start_polling(bot: Bot, dispatcher: Dispatcher) -> None:
    await bot.set_my_commands(BOT_COMMANDS)
    await bot.delete_webhook(drop_pending_updates=False)
    await dispatcher.start_polling(bot, allowed_updates=dispatcher.resolve_used_update_types())


async def _set_webhook(bot: Bot, dispatcher: Dispatcher, settings: Settings) -> None:
    webhook_url = settings.telegram_webhook_url
    if webhook_url is None:
        raise RuntimeError("WEBHOOK_BASE_URL is required in webhook mode")
    await bot.set_my_commands(BOT_COMMANDS)
    await bot.set_webhook(
        webhook_url,
        secret_token=settings.telegram_webhook_secret.get_secret_value()
        if settings.telegram_webhook_secret
        else None,
        allowed_updates=dispatcher.resolve_used_update_types(),
    )


def run() -> None:
    settings = Settings()  # type: ignore[call-arg]  # values come from environment
    logging.basicConfig(level=settings.log_level)
    service = _create_service(settings)
    bot = Bot(settings.telegram_bot_token.get_secret_value())
    dispatcher = create_app(
        service,
        settings,
        CommandParser(
            timezone=settings.timezone,
            default_duration_minutes=settings.default_event_duration_minutes,
        ),
    )

    if settings.bot_mode == "webhook":
        app = build_web_app(bot, dispatcher, settings)
        async def register_webhook(_: web.Application) -> None:
            await _set_webhook(bot, dispatcher, settings)

        app.on_startup.append(register_webhook)
        web.run_app(app, host=settings.host, port=settings.port)
        return

    import asyncio

    asyncio.run(_start_polling(bot, dispatcher))


if __name__ == "__main__":
    run()
