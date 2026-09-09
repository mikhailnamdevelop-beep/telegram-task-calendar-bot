"""Small, transport-facing contracts kept independent from aiogram."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class Button:
    text: str
    data: str


@dataclass(frozen=True, slots=True)
class BotResponse:
    """A service result that can be rendered by Telegram or another transport."""

    text: str
    buttons: Sequence[Button] = field(default_factory=tuple)
    alert: bool = False


class BotItemService(Protocol):
    """The only service API consumed by handlers.

    Implementations must persist any conversational state themselves.  This
    keeps Telegram workers stateless and makes polling and webhook delivery
    behave identically.
    """

    async def add(self, command: Any, *, user_id: int, chat_id: int | None, message_id: int | None) -> Any: ...

    async def agenda(self, command: Any, user_id: int) -> list[Any]: ...

    async def edit(self, command: Any, user_id: int) -> Any: ...

    async def delete(self, item_reference: str, user_id: int, *, scope: str | None = None) -> Any: ...

    async def done(self, item_reference: str, user_id: int) -> Any: ...

    async def undo(self, item_reference: str, user_id: int) -> Any: ...

    async def save_dialog(self, user_id: int, payload: dict[str, Any]) -> None: ...

    async def get_dialog(self, user_id: int) -> dict[str, Any] | None: ...

    async def clear_dialog(self, user_id: int) -> None: ...
