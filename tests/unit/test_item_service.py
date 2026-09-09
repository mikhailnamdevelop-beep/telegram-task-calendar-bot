from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

import pytest

from app.domain import DateRange, Item, ItemStatus, ParsedCommand, Target
from app.services import (
    AmbiguousItemError,
    InvalidItemCommandError,
    ItemNotFoundError,
    ItemService,
)


class FakeRepository:
    def __init__(self) -> None:
        self.items: dict[str, Item] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.dialogs: dict[int, tuple[Mapping[str, Any], datetime]] = {}
        self.status_history: list[ItemStatus] = []

    async def create_item(self, item: Item) -> Item:
        self.items[str(item.id)] = item
        self.status_history.append(item.status)
        return item

    async def get_item(self, item_id: str) -> Item | None:
        return self.items.get(item_id)

    async def get_item_by_reference(self, reference: str, user_id: int) -> Item | None:
        return next(
            (
                item
                for item in self.items.values()
                if item.telegram_user_id == user_id
                and item.deleted_at is None
                and reference in {str(item.id), item.short_id}
            ),
            None,
        )

    async def list_items(self, user_id: int, **kwargs: Any) -> list[Item]:
        start = kwargs.get("start_date")
        end = kwargs.get("end_date")
        statuses = kwargs.get("statuses")
        return [
            item
            for item in self.items.values()
            if item.telegram_user_id == user_id
            and item.deleted_at is None
            and (start is None or item.scheduled_date >= start)
            and (end is None or item.scheduled_date <= end)
            and (not statuses or item.status in statuses)
        ]

    async def update_item(
        self,
        item_id: str,
        user_id: int,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> Item:
        item = self.items[item_id]
        assert item.telegram_user_id == user_id
        assert item.version == expected_version
        updated = item.model_copy(update={**changes, "version": item.version + 1})
        self.items[item_id] = updated
        self.status_history.append(updated.status)
        return updated

    async def soft_delete_item(
        self, item_id: str, user_id: int, *, expected_version: int
    ) -> Item:
        return await self.update_item(
            item_id,
            user_id,
            {"status": ItemStatus.CANCELLED, "deleted_at": datetime.now(timezone.utc)},
            expected_version=expected_version,
        )

    async def set_dialog_state(
        self, user_id: int, payload: Mapping[str, Any], expires_at: datetime
    ) -> None:
        self.dialogs[user_id] = (payload, expires_at)

    async def get_dialog_state(self, user_id: int) -> Mapping[str, Any] | None:
        value = self.dialogs.get(user_id)
        return value[0] if value else None

    async def clear_dialog_state(self, user_id: int) -> None:
        self.dialogs.pop(user_id, None)

    async def get_operation(self, key: str) -> Mapping[str, Any] | None:
        return self.operations.get(key)

    async def begin_operation(
        self, key: str, operation_type: str, **kwargs: Any
    ) -> Mapping[str, Any]:
        return self.operations.setdefault(
            key,
            {
                "idempotency_key": key,
                "operation_type": operation_type,
                "status": "pending",
                **kwargs,
            },
        )

    async def update_operation(
        self, key: str, status: str, **kwargs: Any
    ) -> Mapping[str, Any]:
        self.operations[key].update(status=status, **kwargs)
        return self.operations[key]


class FakeCalendar:
    def __init__(self, *, fail_create: bool = False, fail_delete: bool = False) -> None:
        self.fail_create = fail_create
        self.fail_delete = fail_delete
        self.created: list[Item] = []
        self.deleted: list[str] = []
        self.external: list[dict[str, Any]] = []

    async def create_event(self, item: Item) -> Mapping[str, Any]:
        if self.fail_create:
            raise RuntimeError("temporary Google outage")
        self.created.append(item)
        return {
            "id": f"event-{item.short_id}",
            "htmlLink": "https://calendar.test/event",
        }

    async def update_event(self, event_id: str, item: Item) -> Mapping[str, Any]:
        return {"id": event_id, "htmlLink": "https://calendar.test/event"}

    async def delete_event(self, event_id: str) -> None:
        if self.fail_delete:
            raise RuntimeError("temporary Google outage")
        self.deleted.append(event_id)

    async def list_events(
        self, start: datetime, end: datetime
    ) -> list[Mapping[str, Any]]:
        return self.external

    async def find_event_by_item_id(self, item_id: str) -> Mapping[str, Any] | None:
        return None


def command(
    target: Target = Target.TASK, title: str = "Prepare notes"
) -> ParsedCommand:
    calendar_fields = (
        {"start_time": time(10, 0), "duration_minutes": 30}
        if target in {Target.CALENDAR, Target.BOTH}
        else {}
    )
    return ParsedCommand(
        intent="add",
        target=target,
        title=title,
        scheduled_date=date(2026, 9, 10),
        raw_text=f"{target.value} {title} tomorrow",
        **calendar_fields,
    )


@pytest.mark.asyncio
async def test_add_task_is_idempotent_for_same_telegram_message() -> None:
    repository = FakeRepository()
    service = ItemService(repository, FakeCalendar())

    first = await service.add(command(), user_id=7, chat_id=8, message_id=9)
    second = await service.add(command(), user_id=7, chat_id=8, message_id=9)

    assert first.id == second.id
    assert len(repository.items) == 1


@pytest.mark.asyncio
async def test_both_add_runs_pending_calendar_created_completed_saga() -> None:
    repository = FakeRepository()
    calendar = FakeCalendar()
    service = ItemService(repository, calendar)

    item = await service.add(command(Target.BOTH), user_id=7, chat_id=8, message_id=10)

    assert repository.status_history == [
        ItemStatus.SYNC_PENDING,
        ItemStatus.CALENDAR_CREATED,
        ItemStatus.ACTIVE,
    ]
    assert item.google_event_id == f"event-{item.short_id}"
    operation = repository.operations["telegram:8:10:add"]
    assert operation["status"] == "completed"


@pytest.mark.asyncio
async def test_calendar_failure_remains_retryable() -> None:
    repository = FakeRepository()
    service = ItemService(repository, FakeCalendar(fail_create=True))

    item = await service.add(
        command(Target.CALENDAR), user_id=7, chat_id=8, message_id=11
    )

    assert item.status == ItemStatus.SYNC_FAILED
    assert repository.operations["telegram:8:11:add"]["status"] == "sync_failed"


@pytest.mark.asyncio
async def test_agenda_dedupes_linked_google_event_and_marks_external_read_only() -> (
    None
):
    repository = FakeRepository()
    calendar = FakeCalendar()
    local = Item(
        telegram_user_id=7,
        telegram_chat_id=8,
        type=Target.BOTH,
        title="Linked",
        scheduled_date=date(2026, 9, 10),
        google_event_id="linked-event",
    )
    await repository.create_item(local)
    calendar.external = [
        {"id": "linked-event", "summary": "duplicate", "start": {"date": "2026-09-10"}},
        {"id": "external", "summary": "Dentist", "start": {"date": "2026-09-10"}},
    ]
    service = ItemService(repository, calendar)
    query = ParsedCommand(
        intent="agenda",
        date_range=DateRange(start=date(2026, 9, 10), end=date(2026, 9, 10)),
    )

    entries = await service.agenda(query, 7)

    assert [entry.title for entry in entries] == ["Dentist", "Linked"]
    external = next(entry for entry in entries if entry.title == "Dentist")
    assert external.read_only is True
    assert external.short_id is None


@pytest.mark.asyncio
async def test_title_resolution_reports_ambiguous_candidates() -> None:
    repository = FakeRepository()
    for title in ("Call Mina", "Call bank"):
        await repository.create_item(
            Item(
                telegram_user_id=7,
                telegram_chat_id=8,
                type=Target.TASK,
                title=title,
                scheduled_date=date(2026, 9, 10),
            )
        )
    service = ItemService(repository, FakeCalendar())

    with pytest.raises(AmbiguousItemError) as caught:
        await service.done("call", 7)

    assert len(caught.value.candidates) == 2


@pytest.mark.asyncio
async def test_undo_calendar_api_failure_does_not_hide_item() -> None:
    repository = FakeRepository()
    item = Item(
        telegram_user_id=7,
        telegram_chat_id=8,
        type=Target.BOTH,
        title="Calendar-linked",
        scheduled_date=date(2026, 9, 10),
        google_event_id="event-1",
    )
    await repository.create_item(item)
    service = ItemService(repository, FakeCalendar(fail_delete=True))

    result = await service.undo(item.short_id, 7)

    assert result.status == ItemStatus.SYNC_FAILED
    assert result.deleted_at is None


@pytest.mark.asyncio
async def test_dialog_facade_delegates_with_expiry() -> None:
    repository = FakeRepository()
    service = ItemService(repository, FakeCalendar())

    await service.save_dialog(7, {"step": "confirm"}, ttl=timedelta(minutes=2))

    assert await service.get_dialog(7) == {"step": "confirm"}
    await service.clear_dialog(7)
    assert await service.get_dialog(7) is None


@pytest.mark.asyncio
async def test_invalid_add_and_calendar_without_gateway() -> None:
    repository = FakeRepository()
    service = ItemService(repository, None)

    with pytest.raises(InvalidItemCommandError):
        await service.add(
            ParsedCommand(intent="add", target=Target.TASK),
            user_id=7,
            chat_id=8,
            message_id=20,
        )

    item = await service.add(
        command(Target.CALENDAR), user_id=7, chat_id=8, message_id=21
    )
    assert item.status == ItemStatus.SYNC_FAILED
    assert item.short_id.startswith("C-")


@pytest.mark.asyncio
async def test_retry_sync_recovers_failed_calendar_item() -> None:
    repository = FakeRepository()
    calendar = FakeCalendar(fail_create=True)
    service = ItemService(repository, calendar)
    failed = await service.add(
        command(Target.BOTH), user_id=7, chat_id=8, message_id=22
    )
    calendar.fail_create = False

    recovered = await service.retry_sync(failed.short_id, user_id=7)

    assert recovered.status == ItemStatus.ACTIVE
    assert recovered.google_event_id
    assert recovered.short_id.startswith("B-")


@pytest.mark.asyncio
async def test_edit_task_and_calendar_paths() -> None:
    repository = FakeRepository()
    calendar = FakeCalendar()
    service = ItemService(repository, calendar)
    task = await service.add(command(), user_id=7, chat_id=8, message_id=23)
    edited_task = await service.edit(
        ParsedCommand(
            intent="edit", item_reference=task.short_id, title="New title"
        ),
        7,
    )
    assert edited_task.title == "New title"

    event = await service.add(
        command(Target.CALENDAR), user_id=7, chat_id=8, message_id=24
    )
    edited_event = await service.edit(
        ParsedCommand(
            intent="edit",
            item_reference=event.short_id,
            start_time=time(12),
        ),
        7,
    )
    assert edited_event.start_time == time(12)
    assert edited_event.status == ItemStatus.ACTIVE


@pytest.mark.asyncio
async def test_edit_validation_and_missing_calendar_gateway() -> None:
    repository = FakeRepository()
    task = await repository.create_item(
        Item(
            telegram_user_id=7,
            telegram_chat_id=8,
            type=Target.TASK,
            title="Task",
            scheduled_date=date(2026, 9, 10),
        )
    )
    service = ItemService(repository, None)
    with pytest.raises(InvalidItemCommandError):
        await service.edit(
            ParsedCommand(intent="edit", item_reference=task.short_id), 7
        )

    event = await repository.create_item(
        Item(
            telegram_user_id=7,
            telegram_chat_id=8,
            type=Target.CALENDAR,
            title="Event",
            scheduled_date=date(2026, 9, 10),
            start_time=time(10),
            duration_minutes=60,
        )
    )
    result = await service.edit(
        ParsedCommand(
            intent="edit", item_reference=event.short_id, title="Changed"
        ),
        7,
    )
    assert result.status == ItemStatus.SYNC_FAILED


@pytest.mark.asyncio
async def test_delete_scopes_and_done_validation() -> None:
    repository = FakeRepository()
    calendar = FakeCalendar()
    service = ItemService(repository, calendar)
    linked = await service.add(
        command(Target.BOTH), user_id=7, chat_id=8, message_id=25
    )

    remaining = await service.delete(linked.short_id, 7, scope="calendar")
    assert remaining.type == Target.TASK
    assert remaining.google_event_id is None
    assert calendar.deleted

    with pytest.raises(InvalidItemCommandError):
        await service.delete(remaining.short_id, 7, scope="calendar")

    calendar_only = await service.add(
        command(Target.CALENDAR), user_id=7, chat_id=8, message_id=26
    )
    with pytest.raises(InvalidItemCommandError):
        await service.done(calendar_only.short_id, 7)
    with pytest.raises(InvalidItemCommandError):
        await service.delete(calendar_only.short_id, 7, scope="task")


@pytest.mark.asyncio
async def test_done_then_undo_and_missing_reference() -> None:
    repository = FakeRepository()
    service = ItemService(repository, FakeCalendar())
    item = await service.add(command(), user_id=7, chat_id=8, message_id=27)
    done = await service.done(item.short_id, 7)
    restored = await service.undo(done.short_id, 7)
    assert restored.status == ItemStatus.ACTIVE

    with pytest.raises(ItemNotFoundError):
        await service.done("does-not-exist", 7)


@pytest.mark.asyncio
async def test_agenda_ignores_malformed_external_events() -> None:
    repository = FakeRepository()
    calendar = FakeCalendar()
    calendar.external = [
        {"id": "missing-start", "summary": "bad"},
        {"id": "bad-date", "summary": "bad", "start": {"date": "nope"}},
    ]
    service = ItemService(repository, calendar)
    query = ParsedCommand(
        intent="agenda",
        date_range=DateRange(start=date(2026, 9, 10), end=date(2026, 9, 10)),
    )
    assert await service.agenda(query, 7) == []
