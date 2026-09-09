from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.bot.controller import TelegramController
from app.domain import Intent, ParsedCommand, Target


class FakeParser:
    def __init__(self, command: ParsedCommand) -> None:
        self.command = command
        self.calls: list[str] = []

    def parse(self, text: str) -> ParsedCommand:
        self.calls.append(text)
        return self.command.model_copy(update={"raw_text": text})


class FakeService:
    def __init__(self) -> None:
        self.dialogs: dict[int, dict] = {}
        self.add_calls: list[tuple] = []
        self.deleted: list[str] = []

    async def save_dialog(self, user_id: int, state: dict) -> None:
        self.dialogs[user_id] = state

    async def get_dialog(self, user_id: int) -> dict | None:
        return self.dialogs.get(user_id)

    async def clear_dialog(self, user_id: int) -> None:
        self.dialogs.pop(user_id, None)

    async def add(self, command, *, user_id: int, chat_id: int, message_id: int):
        self.add_calls.append((command, user_id, chat_id, message_id))
        return SimpleNamespace(title=command.title, short_id="a1b2c3d4")

    async def agenda(self, command, user_id: int):
        return []

    async def edit(self, command, user_id: int):
        return SimpleNamespace(title="edited", short_id="a1b2c3d4")

    async def delete(self, reference: str, user_id: int, scope=None):
        self.deleted.append(reference)
        return SimpleNamespace(title="deleted", short_id=reference)

    async def done(self, reference: str, user_id: int):
        return SimpleNamespace(title="done", short_id=reference)

    async def undo(self, reference: str, user_id: int):
        return SimpleNamespace(title="undone", short_id=reference)


def parsed(target: Target = Target.TASK) -> ParsedCommand:
    return ParsedCommand(
        intent=Intent.ADD,
        target=target,
        title="Call Alex",
        scheduled_date=date(2026, 9, 10),
    )


@pytest.mark.asyncio
async def test_plain_text_defaults_to_task_and_offers_undo() -> None:
    service = FakeService()
    parser = FakeParser(parsed())
    controller = TelegramController(service, parser)

    response = await controller.handle_text(
        user_id=7, chat_id=8, message_id=9, text="Call Alex tomorrow"
    )

    assert parser.calls == ["Call Alex tomorrow"]
    assert service.add_calls[0][1:] == (7, 8, 9)
    assert response.text == "Saved: Call Alex [a1b2c3d4]"
    assert response.buttons[0].data == "undo:a1b2c3d4"


@pytest.mark.asyncio
async def test_calendar_request_is_persisted_until_confirmed() -> None:
    service = FakeService()
    controller = TelegramController(service, FakeParser(parsed(Target.CALENDAR)))

    response = await controller.handle_command(
        user_id=7,
        chat_id=8,
        message_id=9,
        command="calendar",
        text="Call Alex tomorrow",
    )

    assert response.buttons[0].data == "confirm:add"
    assert service.add_calls == []
    assert service.dialogs[7]["command"]["target"] == "calendar"

    confirmed = await controller.handle_callback(user_id=7, data="confirm:add")

    assert confirmed.text.startswith("Saved:")
    assert service.add_calls[0][1:] == (7, 8, 9)
    assert 7 not in service.dialogs


@pytest.mark.asyncio
async def test_cancel_clears_persisted_dialog() -> None:
    service = FakeService()
    service.dialogs[7] = {"action": "confirm"}
    controller = TelegramController(service, FakeParser(parsed()))

    response = await controller.handle_command(
        user_id=7, chat_id=8, message_id=9, command="cancel", text=""
    )

    assert response.text == "Cancelled."
    assert service.dialogs == {}
