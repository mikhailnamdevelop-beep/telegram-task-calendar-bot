from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace
from typing import Any

import pytest

from app.bot.controller import TelegramController
from app.domain import ItemStatus, ParsedCommand, Target
from app.parser import CommandParser
from app.services import AmbiguousItemError


class FlowService:
    def __init__(self) -> None:
        self.dialogs: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self.agenda_items: list[Any] = []
        self.delete_candidates: list[Any] | None = None

    @staticmethod
    def item(title: str = "Demo", short_id: str = "T-1234abcd") -> Any:
        return SimpleNamespace(
            title=title,
            short_id=short_id,
            scheduled_date=date(2026, 9, 11),
            start_time=time(15),
            status=ItemStatus.ACTIVE,
        )

    async def save_dialog(self, user_id: int, payload: dict[str, Any]) -> None:
        self.dialogs[user_id] = payload

    async def get_dialog(self, user_id: int) -> dict[str, Any] | None:
        return self.dialogs.get(user_id)

    async def clear_dialog(self, user_id: int) -> None:
        self.dialogs.pop(user_id, None)

    async def add(self, command: ParsedCommand, **kwargs: Any) -> Any:
        self.calls.append(("add", command))
        return self.item(command.title or "Demo")

    async def agenda(self, command: ParsedCommand, user_id: int) -> list[Any]:
        self.calls.append(("agenda", command))
        return self.agenda_items

    async def edit(self, command: ParsedCommand, user_id: int) -> Any:
        self.calls.append(("edit", command))
        return self.item("Edited", command.item_reference or "T-1234abcd")

    async def delete(
        self, reference: str, user_id: int, *, scope: str | None = None
    ) -> Any:
        self.calls.append(("delete", (reference, scope)))
        if self.delete_candidates is not None:
            candidates, self.delete_candidates = self.delete_candidates, None
            raise AmbiguousItemError(candidates)
        return self.item("Deleted", reference)

    async def done(self, reference: str, user_id: int) -> Any:
        self.calls.append(("done", reference))
        return self.item("Done", reference)

    async def undo(self, reference: str, user_id: int) -> Any:
        self.calls.append(("undo", reference))
        return self.item("Undone", reference)


@pytest.mark.asyncio
async def test_natural_calendar_request_is_not_promoted_to_both() -> None:
    service = FlowService()
    controller = TelegramController(service, CommandParser())

    response = await controller.handle_text(
        user_id=7,
        chat_id=8,
        message_id=9,
        text="добавь в календарь завтра в 15:00 встречу",
    )

    assert response.buttons[0].data == "confirm:add"
    assert service.dialogs[7]["command"]["target"] == "calendar"
    assert not service.calls


@pytest.mark.asyncio
async def test_plain_text_defaults_to_task_with_real_parser() -> None:
    service = FlowService()
    controller = TelegramController(service, CommandParser())

    response = await controller.handle_text(
        user_id=7, chat_id=8, message_id=9, text="завтра купить лекарства"
    )

    assert service.calls[0][0] == "add"
    assert service.calls[0][1].target == Target.TASK
    assert response.buttons[0].data.startswith("undo:")


@pytest.mark.asyncio
async def test_clarification_is_persisted_and_completed() -> None:
    service = FlowService()
    controller = TelegramController(service, CommandParser())

    first = await controller.handle_command(
        user_id=7,
        chat_id=8,
        message_id=9,
        command="calendar",
        text="завтра врач",
    )
    assert "start_time" in first.text
    assert service.dialogs[7]["action"] == "clarify"

    second = await controller.handle_text(
        user_id=7, chat_id=8, message_id=10, text="15:00"
    )
    assert second.buttons[0].data == "confirm:add"
    assert service.dialogs[7]["command"]["start_time"] == "15:00:00"


@pytest.mark.asyncio
async def test_natural_agenda_and_nonempty_rendering() -> None:
    service = FlowService()
    service.agenda_items = [service.item("Doctor", "C-1234abcd")]
    controller = TelegramController(service, CommandParser())

    response = await controller.handle_text(
        user_id=7, chat_id=8, message_id=9, text="что у меня завтра"
    )

    assert service.calls[0][0] == "agenda"
    assert "Doctor" in response.text
    assert "15:00" in response.text


@pytest.mark.asyncio
async def test_edit_and_delete_wait_for_confirmation() -> None:
    service = FlowService()
    controller = TelegramController(service, CommandParser())

    edit = await controller.handle_command(
        user_id=7,
        chat_id=8,
        message_id=9,
        command="edit",
        text="T-1234abcd перенести на послезавтра",
    )
    assert edit.buttons[0].data == "confirm:edit"
    assert not service.calls
    updated = await controller.handle_callback(user_id=7, data="confirm:edit")
    assert updated.text.startswith("Updated:")

    delete = await controller.handle_command(
        user_id=7,
        chat_id=8,
        message_id=10,
        command="delete",
        text="T-1234abcd",
    )
    assert delete.buttons[0].data == "confirm:delete"
    deleted = await controller.handle_callback(user_id=7, data="confirm:delete")
    assert deleted.text.startswith("Deleted:")


@pytest.mark.asyncio
async def test_ambiguous_delete_uses_candidate_callback() -> None:
    service = FlowService()
    service.delete_candidates = [
        service.item("Call Mina", "T-1111aaaa"),
        service.item("Call bank", "T-2222bbbb"),
    ]
    controller = TelegramController(service, CommandParser())

    await controller.handle_command(
        user_id=7, chat_id=8, message_id=9, command="delete", text="call"
    )
    choices = await controller.handle_callback(user_id=7, data="confirm:delete")
    assert [button.data for button in choices.buttons] == [
        "candidate:T-1111aaaa",
        "candidate:T-2222bbbb",
    ]

    result = await controller.handle_callback(
        user_id=7, data="candidate:T-2222bbbb"
    )
    assert result.text.startswith("Delete:")


@pytest.mark.asyncio
async def test_done_undo_cancel_expired_and_unknown_callbacks() -> None:
    service = FlowService()
    controller = TelegramController(service, CommandParser())

    done = await controller.handle_command(
        user_id=7, chat_id=8, message_id=9, command="done", text="T-1234abcd"
    )
    assert done.text.startswith("Done:")
    undone = await controller.handle_callback(user_id=7, data="undo:T-1234abcd")
    assert undone.text.startswith("Undone:")
    expired = await controller.handle_callback(user_id=7, data="confirm:add")
    assert expired.alert is True
    unknown = await controller.handle_callback(user_id=7, data="unknown:value")
    assert unknown.alert is True
    cancelled = await controller.handle_callback(user_id=7, data="cancel")
    assert cancelled.text == "Cancelled."


@pytest.mark.asyncio
async def test_help_unknown_empty_and_empty_agenda() -> None:
    service = FlowService()
    controller = TelegramController(service, CommandParser())

    help_response = await controller.handle_command(
        user_id=7, chat_id=8, message_id=1, command="help", text=""
    )
    assert "/calendar" in help_response.text
    unknown = await controller.handle_command(
        user_id=7, chat_id=8, message_id=2, command="wat", text="x"
    )
    assert "Unknown" in unknown.text
    empty = await controller.handle_command(
        user_id=7, chat_id=8, message_id=3, command="task", text=""
    )
    assert "Add text" in empty.text
    agenda = await controller.handle_command(
        user_id=7, chat_id=8, message_id=4, command="today", text=""
    )
    assert agenda.text == "Nothing scheduled."
