"""Authorization middleware shared by messages and callback queries."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject


class AllowlistMiddleware(BaseMiddleware):
    """Silently reject updates that are not from an explicitly allowed user."""

    def __init__(self, allowed_user_ids: set[int]) -> None:
        self.allowed_user_ids = allowed_user_ids

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from_user = getattr(event, "from_user", None)
        if from_user is not None and from_user.id in self.allowed_user_ids:
            return await handler(event, data)

        # Acknowledge callbacks so Telegram does not leave the client spinner on.
        if isinstance(event, CallbackQuery):
            await event.answer("Access denied", show_alert=True)
        elif isinstance(event, Message):
            await event.answer("Access denied")
        return None
