"""Thin aiogram handlers. Item parsing and state live in ItemService."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message

from .controller import TelegramController
from .middleware import AllowlistMiddleware
from .rendering import coerce_response, markup

COMMANDS = ("start", "task", "calendar", "both", "agenda", "edit", "delete", "done", "today", "tomorrow", "week", "cancel", "help")


def create_router(controller: TelegramController, allowed_user_ids: set[int]) -> Router:
    router = Router(name="bot")
    allowlist = AllowlistMiddleware(allowed_user_ids)
    router.message.outer_middleware(allowlist)
    router.callback_query.outer_middleware(allowlist)

    @router.message(Command(commands=list(COMMANDS)))
    async def command(message: Message, command: CommandObject) -> None:
        if message.from_user is None:
            return
        response = coerce_response(
            await controller.handle_command(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                message_id=message.message_id,
                command=command.command.lower(),
                text=command.args or "",
            )
        )
        await message.answer(response.text, reply_markup=markup(response))

    @router.message(F.text & ~F.text.startswith("/"))
    async def plain_text(message: Message) -> None:
        if message.from_user is None or message.text is None:
            return
        response = coerce_response(
            await controller.handle_text(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                message_id=message.message_id,
                text=message.text,
            )
        )
        await message.answer(response.text, reply_markup=markup(response))

    @router.callback_query()
    async def callback(query: CallbackQuery) -> None:
        response = coerce_response(
            await controller.handle_callback(user_id=query.from_user.id, data=query.data or "")
        )
        await query.answer(response.text if response.alert else None, show_alert=response.alert)
        if query.message is not None:
            await query.message.answer(response.text, reply_markup=markup(response))

    return router
