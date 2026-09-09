from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any, Protocol

from app.domain import Item, ItemStatus


class ItemRepository(Protocol):
    async def create_item(self, item: Item) -> Item: ...

    async def get_item(self, item_id: str) -> Item | None: ...

    async def get_item_by_reference(
        self, reference: str, user_id: int
    ) -> Item | None: ...

    async def list_items(
        self,
        user_id: int,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        statuses: Sequence[ItemStatus | str] | None = None,
        include_deleted: bool = False,
    ) -> list[Item]: ...

    async def update_item(
        self,
        item_id: str,
        user_id: int,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> Item: ...

    async def soft_delete_item(
        self, item_id: str, user_id: int, *, expected_version: int
    ) -> Item: ...

    async def set_dialog_state(
        self,
        user_id: int,
        payload: Mapping[str, Any],
        expires_at: datetime,
        *,
        chat_id: int | None = None,
    ) -> None: ...

    async def get_dialog_state(
        self, user_id: int, *, chat_id: int | None = None
    ) -> Mapping[str, Any] | None: ...

    async def clear_dialog_state(
        self, user_id: int, *, chat_id: int | None = None
    ) -> None: ...

    async def get_operation(self, idempotency_key: str) -> Mapping[str, Any] | None: ...

    async def begin_operation(
        self,
        idempotency_key: str,
        operation_type: str,
        *,
        item_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        user_id: int,
        chat_id: int | None = None,
    ) -> Mapping[str, Any]: ...

    async def update_operation(
        self,
        idempotency_key: str,
        status: str,
        *,
        item_id: str | None = None,
        error: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]: ...


class CalendarGateway(Protocol):
    async def create_event(self, item: Item) -> Mapping[str, Any]: ...

    async def update_event(self, event_id: str, item: Item) -> Mapping[str, Any]: ...

    async def delete_event(self, event_id: str) -> None: ...

    async def list_events(
        self, start: datetime, end: datetime
    ) -> list[Mapping[str, Any]]: ...

    async def find_event_by_item_id(self, item_id: str) -> Mapping[str, Any] | None: ...
