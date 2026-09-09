from __future__ import annotations

from datetime import date, time
from types import SimpleNamespace
from typing import Any

import pytest

from app.adapters.google_calendar import GoogleCalendarAdapter
from app.adapters.supabase import SupabaseRepository, VersionConflictError
from app.domain import Item, Target


class Request:
    def __init__(self, result: Any = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error

    def execute(self) -> Any:
        if self.error:
            raise self.error
        return self.result


class EventsResource:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.list_results: list[dict[str, Any]] = []
        self.insert_errors: list[Exception] = []

    def list(self, **kwargs: Any) -> Request:
        self.calls.append(("list", kwargs))
        result = self.list_results.pop(0) if self.list_results else {"items": []}
        return Request(result)

    def insert(self, **kwargs: Any) -> Request:
        self.calls.append(("insert", kwargs))
        if self.insert_errors:
            return Request(error=self.insert_errors.pop(0))
        return Request({"id": "new-event", "htmlLink": "https://calendar.test/new"})

    def patch(self, **kwargs: Any) -> Request:
        self.calls.append(("patch", kwargs))
        return Request({"id": kwargs["eventId"]})

    def delete(self, **kwargs: Any) -> Request:
        self.calls.append(("delete", kwargs))
        return Request({})


class GoogleService:
    def __init__(self, events: EventsResource) -> None:
        self._events = events

    def events(self) -> EventsResource:
        return self._events


@pytest.mark.asyncio
async def test_google_create_uses_private_item_id_and_seoul_timezone() -> None:
    events = EventsResource()
    adapter = GoogleCalendarAdapter(service=GoogleService(events))
    item = Item(
        telegram_user_id=1,
        telegram_chat_id=1,
        type=Target.CALENDAR,
        title="Meeting",
        scheduled_date=date(2026, 9, 10),
        start_time=time(14, 30),
        duration_minutes=30,
    )

    result = await adapter.create_event(item)

    assert result["id"] == "new-event"
    _, insert = next(call for call in events.calls if call[0] == "insert")
    body = insert["body"]
    assert body["start"]["timeZone"] == "Asia/Seoul"
    assert body["extendedProperties"]["private"]["item_id"] == str(item.id)


@pytest.mark.asyncio
async def test_google_create_returns_existing_event_without_insert() -> None:
    events = EventsResource()
    events.list_results.append({"items": [{"id": "already-there"}]})
    adapter = GoogleCalendarAdapter(service=GoogleService(events))
    item = Item(
        telegram_user_id=1,
        telegram_chat_id=1,
        type=Target.CALENDAR,
        title="Meeting",
        scheduled_date=date(2026, 9, 10),
    )

    result = await adapter.create_event(item)

    assert result["id"] == "already-there"
    assert [name for name, _ in events.calls] == ["list"]


@pytest.mark.asyncio
async def test_google_create_checks_dedupe_after_uncertain_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RetryableError(RuntimeError):
        resp = SimpleNamespace(status=503)

    async def no_sleep(_: float) -> None:
        return None

    monkeypatch.setattr("app.adapters.google_calendar.asyncio.sleep", no_sleep)
    events = EventsResource()
    events.list_results.extend(
        [
            {"items": []},
            {"items": [{"id": "created-despite-timeout"}]},
        ]
    )
    events.insert_errors.append(RetryableError("response lost"))
    adapter = GoogleCalendarAdapter(service=GoogleService(events))
    item = Item(
        telegram_user_id=1,
        telegram_chat_id=1,
        type=Target.CALENDAR,
        title="Meeting",
        scheduled_date=date(2026, 9, 10),
    )

    result = await adapter.create_event(item)

    assert result["id"] == "created-despite-timeout"
    assert [name for name, _ in events.calls].count("insert") == 1


class Query:
    def __init__(self, table: Table) -> None:
        self.table = table
        self.filters: dict[str, Any] = {}
        self.payload: dict[str, Any] | None = None

    def select(self, *_: Any) -> Query:
        return self

    def limit(self, *_: Any) -> Query:
        return self

    def is_(self, *_: Any) -> Query:
        return self

    def order(self, *_: Any) -> Query:
        return self

    def gte(self, *_: Any) -> Query:
        return self

    def lte(self, *_: Any) -> Query:
        return self

    def in_(self, *_: Any) -> Query:
        return self

    def eq(self, key: str, value: Any) -> Query:
        self.filters[key] = value
        return self

    def update(self, payload: dict[str, Any]) -> Query:
        self.payload = payload
        return self

    def execute(self) -> SimpleNamespace:
        rows = [
            row
            for row in self.table.rows
            if all(row.get(k) == v for k, v in self.filters.items())
        ]
        if self.payload is not None:
            for row in rows:
                row.update(self.payload)
        return SimpleNamespace(data=[dict(row) for row in rows])


class Table:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def select(self, *args: Any) -> Query:
        return Query(self).select(*args)

    def update(self, payload: dict[str, Any]) -> Query:
        return Query(self).update(payload)


class SupabaseClient:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.items = Table(rows)

    def table(self, name: str) -> Table:
        assert name == "items"
        return self.items


@pytest.mark.asyncio
async def test_supabase_version_aware_update_rejects_stale_version() -> None:
    item = Item(
        telegram_user_id=1,
        telegram_chat_id=1,
        type=Target.TASK,
        title="Old",
        scheduled_date=date(2026, 9, 10),
        version=2,
    )
    row = item.model_dump(mode="json", by_alias=True)
    repository = SupabaseRepository(client=SupabaseClient([row]))

    with pytest.raises(VersionConflictError):
        await repository.update_item(
            str(item.id), 1, {"title": "New"}, expected_version=1
        )
