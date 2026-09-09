from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

from app.adapters.protocols import CalendarGateway, ItemRepository
from app.domain import Item, ItemStatus, ItemType, ParsedCommand

from .errors import AmbiguousItemError, InvalidItemCommandError, ItemNotFoundError


@dataclass(frozen=True, slots=True)
class AgendaEntry:
    id: str
    short_id: str | None
    title: str
    scheduled_date: date
    start_time: time | None
    duration_minutes: int | None
    source: str
    status: str
    google_event_id: str | None = None
    html_link: str | None = None
    read_only: bool = False


class ItemService:
    def __init__(
        self,
        repository: ItemRepository,
        calendar: CalendarGateway | None,
        timezone: str = "Asia/Seoul",
        dialog_ttl_seconds: int = 1800,
    ):
        self.repository = repository
        self.calendar = calendar
        self.timezone = timezone
        self._zone = ZoneInfo(timezone)
        self._dialog_ttl = timedelta(seconds=dialog_ttl_seconds)

    async def add(
        self,
        command: ParsedCommand,
        *,
        user_id: int,
        chat_id: int | None,
        message_id: int | None,
    ) -> Item:
        if not command.title or not command.scheduled_date or not command.target:
            raise InvalidItemCommandError(
                "title, scheduled_date, and target are required"
            )
        item_type = ItemType(command.target.value)
        requires_calendar = item_type in {ItemType.CALENDAR, ItemType.BOTH}
        if requires_calendar and (
            command.start_time is None or command.duration_minutes is None
        ):
            raise InvalidItemCommandError(
                "calendar items require start_time and duration_minutes"
            )
        idempotency_key = (
            f"telegram:{chat_id or user_id}:{message_id}:add"
            if message_id is not None
            else f"service:{uuid4()}:add"
        )
        operation = await self.repository.begin_operation(
            idempotency_key,
            "add",
            payload={"user_id": user_id, "raw_text": command.raw_text},
            user_id=user_id,
            chat_id=chat_id,
        )
        if operation.get("item_id"):
            existing = await self.repository.get_item(str(operation["item_id"]))
            if existing is not None:
                if operation.get("status") in {"completed", "succeeded"}:
                    return existing
                if requires_calendar and existing.status in {
                    ItemStatus.SYNC_PENDING,
                    ItemStatus.CALENDAR_CREATED,
                    ItemStatus.SYNC_FAILED,
                }:
                    if self.calendar is None:
                        return existing
                    return await self._create_calendar_saga(existing, idempotency_key)
                return existing
        item = Item(
            short_id=f"{item_type.value[0].upper()}-{uuid4().hex[:8]}",
            telegram_user_id=user_id,
            telegram_chat_id=chat_id if chat_id is not None else user_id,
            telegram_message_id=message_id,
            type=item_type,
            title=command.title,
            scheduled_date=command.scheduled_date,
            start_time=command.start_time,
            duration_minutes=command.duration_minutes,
            timezone=self.timezone,
            status=ItemStatus.SYNC_PENDING if requires_calendar else ItemStatus.ACTIVE,
        )
        item = await self.repository.create_item(item)
        await self.repository.update_operation(
            idempotency_key, "pending", item_id=str(item.id)
        )
        if not requires_calendar:
            await self.repository.update_operation(
                idempotency_key, "completed", item_id=str(item.id)
            )
            return item
        if self.calendar is None:
            return await self._sync_failed(
                item, idempotency_key, "Calendar is not configured"
            )
        return await self._create_calendar_saga(item, idempotency_key)

    async def _create_calendar_saga(self, item: Item, idempotency_key: str) -> Item:
        assert self.calendar is not None
        try:
            event = await self.calendar.create_event(item)
            item = await self.repository.update_item(
                str(item.id),
                item.telegram_user_id,
                {
                    "status": ItemStatus.CALENDAR_CREATED,
                    "google_event_id": event.get("id"),
                    "google_event_html_link": event.get("htmlLink"),
                },
                expected_version=item.version,
            )
            await self.repository.update_operation(
                idempotency_key,
                "calendar_created",
                item_id=str(item.id),
                payload={
                    "saga_stage": "calendar_created",
                    "google_event_id": item.google_event_id,
                },
            )
            item = await self.repository.update_item(
                str(item.id),
                item.telegram_user_id,
                {"status": ItemStatus.ACTIVE},
                expected_version=item.version,
            )
            await self.repository.update_operation(
                idempotency_key, "completed", item_id=str(item.id)
            )
            return item
        except Exception as exc:  # noqa: BLE001 - gateway implementations vary
            return await self._sync_failed(
                item,
                idempotency_key,
                f"{type(exc).__name__}: calendar operation failed",
            )

    async def _sync_failed(self, item: Item, key: str, error: str) -> Item:
        try:
            item = await self.repository.update_item(
                str(item.id),
                item.telegram_user_id,
                {"status": ItemStatus.SYNC_FAILED},
                expected_version=item.version,
            )
        except Exception:  # noqa: BLE001 - best-effort recovery must preserve original error
            # Preserve the original failure; a retry can recover by private item_id.
            refreshed = await self.repository.get_item(str(item.id))
            if refreshed is not None:
                item = refreshed
        await self.repository.update_operation(
            key, "sync_failed", item_id=str(item.id), error=error[:2000]
        )
        return item

    async def retry_sync(self, item_reference: str, *, user_id: int) -> Item:
        item = await self._resolve(item_reference, user_id)
        if item.status not in {
            ItemStatus.SYNC_FAILED,
            ItemStatus.SYNC_PENDING,
            ItemStatus.CALENDAR_CREATED,
        }:
            return item
        if self.calendar is None:
            return item
        key = f"retry:{item.id}:{uuid4()}"
        await self.repository.begin_operation(
            key,
            "update",
            item_id=str(item.id),
            user_id=user_id,
            chat_id=item.telegram_chat_id,
            payload={"reason": "retry_sync"},
        )
        return await self._create_calendar_saga(item, key)

    async def agenda(self, command: ParsedCommand, user_id: int) -> list[AgendaEntry]:
        if command.date_range:
            start_date, end_date = command.date_range.start, command.date_range.end
        elif command.scheduled_date:
            start_date = end_date = command.scheduled_date
        else:
            start_date = end_date = datetime.now(self._zone).date()
        local = await self.repository.list_items(
            user_id,
            start_date=start_date,
            end_date=end_date,
            statuses=[
                ItemStatus.ACTIVE,
                ItemStatus.SYNC_PENDING,
                ItemStatus.CALENDAR_CREATED,
                ItemStatus.SYNC_FAILED,
            ],
        )
        entries = [self._local_entry(item) for item in local]
        linked_event_ids = {
            item.google_event_id for item in local if item.google_event_id
        }
        if self.calendar is not None:
            starts_at = datetime.combine(start_date, time.min, self._zone)
            ends_at = datetime.combine(
                end_date + timedelta(days=1), time.min, self._zone
            )
            for event in await self.calendar.list_events(starts_at, ends_at):
                event_id = str(event.get("id", ""))
                if not event_id or event_id in linked_event_ids:
                    continue
                external = self._external_entry(event)
                if external is not None:
                    entries.append(external)
        return sorted(
            entries,
            key=lambda entry: (
                entry.scheduled_date,
                entry.start_time or time.min,
                entry.title.casefold(),
            ),
        )

    async def edit(self, command: ParsedCommand, user_id: int) -> Item:
        if not command.item_reference:
            raise InvalidItemCommandError("item_reference is required")
        item = await self._resolve(command.item_reference, user_id)
        changes: dict[str, Any] = {}
        for name in ("title", "scheduled_date", "start_time", "duration_minutes"):
            value = getattr(command, name)
            if value is not None:
                changes[name] = value
        if not changes:
            raise InvalidItemCommandError("edit contains no changes")
        calendar_item = item.type in {ItemType.CALENDAR, ItemType.BOTH}
        if calendar_item:
            changes["status"] = ItemStatus.SYNC_PENDING
        item = await self.repository.update_item(
            str(item.id), user_id, changes, expected_version=item.version
        )
        if not calendar_item:
            return item
        if self.calendar is None:
            return await self.repository.update_item(
                str(item.id),
                user_id,
                {"status": ItemStatus.SYNC_FAILED},
                expected_version=item.version,
            )
        try:
            event = None
            if item.google_event_id:
                event = await self.calendar.update_event(item.google_event_id, item)
            else:
                event = await self.calendar.create_event(item)
            return await self.repository.update_item(
                str(item.id),
                user_id,
                {
                    "status": ItemStatus.ACTIVE,
                    "google_event_id": event.get("id"),
                    "google_event_html_link": event.get("htmlLink"),
                },
                expected_version=item.version,
            )
        except Exception:  # noqa: BLE001 - external gateway exceptions are implementation-specific
            return await self.repository.update_item(
                str(item.id),
                user_id,
                {"status": ItemStatus.SYNC_FAILED},
                expected_version=item.version,
            )

    async def delete(
        self,
        item_reference: str,
        user_id: int,
        *,
        scope: str | None = None,
    ) -> Item:
        item = await self._resolve(item_reference, user_id)
        normalized_scope = scope.casefold() if scope else None
        if normalized_scope not in {None, "task", "calendar", "both"}:
            raise InvalidItemCommandError("scope must be task, calendar, or both")
        if item.type == ItemType.TASK and normalized_scope == "calendar":
            raise InvalidItemCommandError("a task has no calendar part")
        if item.type == ItemType.CALENDAR and normalized_scope == "task":
            raise InvalidItemCommandError("a calendar item has no task part")
        if (
            item.google_event_id
            and normalized_scope != "task"
            and self.calendar is not None
        ):
            await self.calendar.delete_event(item.google_event_id)
        if item.type == ItemType.BOTH and normalized_scope in {"task", "calendar"}:
            remaining_type = (
                ItemType.CALENDAR if normalized_scope == "task" else ItemType.TASK
            )
            return await self.repository.update_item(
                str(item.id),
                user_id,
                {
                    "type": remaining_type,
                    "google_event_id": None
                    if normalized_scope == "calendar"
                    else item.google_event_id,
                    "google_event_html_link": None
                    if normalized_scope == "calendar"
                    else item.google_event_html_link,
                },
                expected_version=item.version,
            )
        return await self.repository.soft_delete_item(
            str(item.id), user_id, expected_version=item.version
        )

    async def done(self, item_reference: str, user_id: int) -> Item:
        item = await self._resolve(item_reference, user_id)
        if item.type == ItemType.CALENDAR:
            raise InvalidItemCommandError("calendar-only items cannot be completed")
        return await self.repository.update_item(
            str(item.id),
            user_id,
            {"status": ItemStatus.DONE},
            expected_version=item.version,
        )

    async def undo(self, item_reference: str, user_id: int) -> Item:
        item = await self._resolve(item_reference, user_id)
        if item.status == ItemStatus.DONE:
            return await self.repository.update_item(
                str(item.id),
                user_id,
                {"status": ItemStatus.ACTIVE},
                expected_version=item.version,
            )
        if item.type in {ItemType.CALENDAR, ItemType.BOTH} and item.google_event_id:
            if self.calendar is None:
                return await self.repository.update_item(
                    str(item.id),
                    user_id,
                    {"status": ItemStatus.SYNC_FAILED},
                    expected_version=item.version,
                )
            try:
                assert self.calendar is not None
                await self.calendar.delete_event(item.google_event_id)
            except Exception:  # noqa: BLE001 - external gateway exceptions are implementation-specific
                return await self.repository.update_item(
                    str(item.id),
                    user_id,
                    {"status": ItemStatus.SYNC_FAILED},
                    expected_version=item.version,
                )
        return await self.repository.soft_delete_item(
            str(item.id), user_id, expected_version=item.version
        )

    async def save_dialog(
        self,
        user_id: int,
        payload: Mapping[str, Any],
        *,
        ttl: timedelta | None = None,
    ) -> None:
        await self.repository.set_dialog_state(
            user_id, payload, datetime.now(self._zone) + (ttl or self._dialog_ttl)
        )

    async def get_dialog(self, user_id: int) -> Mapping[str, Any] | None:
        return await self.repository.get_dialog_state(user_id)

    async def clear_dialog(self, user_id: int) -> None:
        await self.repository.clear_dialog_state(user_id)

    async def _resolve(self, reference: str, user_id: int) -> Item:
        direct = await self.repository.get_item_by_reference(reference, user_id)
        if direct is not None:
            return direct
        items = await self.repository.list_items(user_id)
        needle = " ".join(reference.casefold().split())
        exact = [
            item for item in items if " ".join(item.title.casefold().split()) == needle
        ]
        candidates = exact or [
            item for item in items if needle in item.title.casefold()
        ]
        if not candidates:
            raise ItemNotFoundError(reference)
        if len(candidates) > 1:
            raise AmbiguousItemError(candidates)
        return candidates[0]

    @staticmethod
    def _local_entry(item: Item) -> AgendaEntry:
        return AgendaEntry(
            id=str(item.id),
            short_id=item.short_id,
            title=item.title,
            scheduled_date=item.scheduled_date,
            start_time=item.start_time,
            duration_minutes=item.duration_minutes,
            source="local",
            status=item.status.value,
            google_event_id=item.google_event_id,
            html_link=item.google_event_html_link,
        )

    def _external_entry(self, event: Mapping[str, Any]) -> AgendaEntry | None:
        start = event.get("start") or {}
        try:
            if start.get("dateTime"):
                starts_at = datetime.fromisoformat(
                    str(start["dateTime"]).replace("Z", "+00:00")
                )
                starts_at = starts_at.astimezone(self._zone)
                scheduled_date, start_time = (
                    starts_at.date(),
                    starts_at.time().replace(tzinfo=None),
                )
            elif start.get("date"):
                scheduled_date, start_time = (
                    date.fromisoformat(str(start["date"])),
                    None,
                )
            else:
                return None
        except (TypeError, ValueError):
            return None
        return AgendaEntry(
            id=f"google:{event['id']}",
            short_id=None,
            title=str(event.get("summary") or "(untitled event)"),
            scheduled_date=scheduled_date,
            start_time=start_time,
            duration_minutes=None,
            source="google",
            status=str(event.get("status") or "confirmed"),
            google_event_id=str(event["id"]),
            html_link=event.get("htmlLink"),
            read_only=True,
        )
