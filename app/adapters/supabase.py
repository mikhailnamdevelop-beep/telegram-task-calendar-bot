from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import date, datetime, timezone
from typing import Any, TypeVar, cast
from uuid import UUID

from app.domain import Item, ItemStatus

T = TypeVar("T")


class VersionConflictError(RuntimeError):
    """The item changed after the caller read it."""


class ItemNotFoundError(LookupError):
    pass


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


class SupabaseRepository:
    """Async facade over the official synchronous Supabase Python client."""

    def __init__(
        self,
        url: str | None = None,
        key: str | None = None,
        *,
        client: Any | None = None,
    ):
        if client is None:
            if not url or not key:
                raise ValueError("Supabase URL and key are required")
            from supabase import create_client

            client = create_client(url, key)
        self._client = client

    @classmethod
    def from_credentials(cls, url: str, service_key: str) -> SupabaseRepository:
        return cls(url, service_key)

    async def _run(self, operation: Callable[[], T]) -> T:
        return await asyncio.to_thread(operation)

    @staticmethod
    def _item_payload(item: Item) -> dict[str, Any]:
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json", by_alias=True)
        raise TypeError("Item must support Pydantic model_dump")

    @staticmethod
    def _item(row: Mapping[str, Any]) -> Item:
        return Item.model_validate(dict(row))

    async def create_item(self, item: Item) -> Item:
        payload = self._item_payload(item)

        def execute() -> Any:
            return self._client.table("items").insert(payload).execute()

        response = await self._run(execute)
        rows = response.data or []
        return self._item(rows[0] if rows else payload)

    async def get_item(self, item_id: str) -> Item | None:
        def execute() -> Any:
            return (
                self._client.table("items")
                .select("*")
                .eq("id", str(item_id))
                .limit(1)
                .execute()
            )

        response = await self._run(execute)
        return self._item(response.data[0]) if response.data else None

    async def get_item_by_reference(self, reference: str, user_id: int) -> Item | None:
        reference = reference.strip()

        def execute() -> Any:
            query = (
                self._client.table("items")
                .select("*")
                .eq("telegram_user_id", user_id)
                .is_("deleted_at", "null")
            )
            try:
                UUID(reference)
                field = "id"
            except ValueError:
                field = "short_id"
            return query.eq(field, reference).limit(1).execute()

        response = await self._run(execute)
        return self._item(response.data[0]) if response.data else None

    async def list_items(
        self,
        user_id: int,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        statuses: Sequence[ItemStatus | str] | None = None,
        include_deleted: bool = False,
    ) -> list[Item]:
        def execute() -> Any:
            query = (
                self._client.table("items").select("*").eq("telegram_user_id", user_id)
            )
            if not include_deleted:
                query = query.is_("deleted_at", "null")
            if start_date:
                query = query.gte("scheduled_date", start_date.isoformat())
            if end_date:
                query = query.lte("scheduled_date", end_date.isoformat())
            if statuses:
                query = query.in_(
                    "status", [_enum_value(status) for status in statuses]
                )
            return query.order("scheduled_date").order("start_time").execute()

        response = await self._run(execute)
        return [self._item(row) for row in (response.data or [])]

    async def update_item(
        self,
        item_id: str,
        user_id: int,
        changes: Mapping[str, Any],
        *,
        expected_version: int,
    ) -> Item:
        payload: dict[str, Any] = {}
        for key, value in changes.items():
            db_key = "item_type" if key == "type" else key
            if hasattr(value, "isoformat"):
                value = value.isoformat()
            payload[db_key] = _enum_value(value)
        payload["version"] = expected_version + 1
        payload["updated_at"] = datetime.now(timezone.utc).isoformat()

        def execute() -> Any:
            return (
                self._client.table("items")
                .update(payload)
                .eq("id", str(item_id))
                .eq("telegram_user_id", user_id)
                .eq("version", expected_version)
                .is_("deleted_at", "null")
                .execute()
            )

        response = await self._run(execute)
        if not response.data:
            existing = await self.get_item(str(item_id))
            if existing is None or existing.telegram_user_id != user_id:
                raise ItemNotFoundError(str(item_id))
            raise VersionConflictError(
                f"Item {item_id} expected version {expected_version}, found {existing.version}"
            )
        return self._item(response.data[0])

    async def soft_delete_item(
        self, item_id: str, user_id: int, *, expected_version: int
    ) -> Item:
        return await self.update_item(
            item_id,
            user_id,
            {
                "status": ItemStatus.CANCELLED,
                "deleted_at": datetime.now(timezone.utc),
            },
            expected_version=expected_version,
        )

    async def set_dialog_state(
        self,
        user_id: int,
        payload: Mapping[str, Any],
        expires_at: datetime,
        *,
        chat_id: int | None = None,
    ) -> None:
        row = {
            "telegram_chat_id": chat_id if chat_id is not None else user_id,
            "telegram_user_id": user_id,
            "payload": dict(payload),
            "expires_at": expires_at.isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

        def execute() -> Any:
            return (
                self._client.table("dialog_states")
                .upsert(
                    cast(Any, row),
                    on_conflict="telegram_chat_id,telegram_user_id",
                )
                .execute()
            )

        await self._run(execute)

    async def get_dialog_state(
        self, user_id: int, *, chat_id: int | None = None
    ) -> Mapping[str, Any] | None:
        def execute() -> Any:
            return (
                self._client.table("dialog_states")
                .select("payload,expires_at")
                .eq("telegram_chat_id", chat_id if chat_id is not None else user_id)
                .eq("telegram_user_id", user_id)
                .limit(1)
                .execute()
            )

        response = await self._run(execute)
        if not response.data:
            return None
        row = response.data[0]
        expires_at = datetime.fromisoformat(
            str(row["expires_at"]).replace("Z", "+00:00")
        )
        now = datetime.now(expires_at.tzinfo or timezone.utc)
        if expires_at <= now:
            await self.clear_dialog_state(user_id, chat_id=chat_id)
            return None
        return row["payload"]

    async def clear_dialog_state(
        self, user_id: int, *, chat_id: int | None = None
    ) -> None:
        def execute() -> Any:
            return (
                self._client.table("dialog_states")
                .delete()
                .eq("telegram_chat_id", chat_id if chat_id is not None else user_id)
                .eq("telegram_user_id", user_id)
                .execute()
            )

        await self._run(execute)

    async def get_operation(self, idempotency_key: str) -> Mapping[str, Any] | None:
        def execute() -> Any:
            return (
                self._client.table("operations")
                .select("*")
                .eq("idempotency_key", idempotency_key)
                .limit(1)
                .execute()
            )

        response = await self._run(execute)
        return response.data[0] if response.data else None

    async def begin_operation(
        self,
        idempotency_key: str,
        operation_type: str,
        *,
        item_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        user_id: int,
        chat_id: int | None = None,
    ) -> Mapping[str, Any]:
        existing = await self.get_operation(idempotency_key)
        if existing:
            return existing
        row = {
            "idempotency_key": idempotency_key,
            "operation_type": operation_type,
            "status": "pending",
            "telegram_user_id": user_id,
            "telegram_chat_id": chat_id,
            "item_id": item_id,
            "payload": dict(payload or {}),
            "error_message": None,
        }

        def execute() -> Any:
            return self._client.table("operations").insert(cast(Any, row)).execute()

        try:
            response = await self._run(execute)
            return response.data[0] if response.data else row
        except Exception:
            # A concurrent delivery may have won the unique idempotency key race.
            existing = await self.get_operation(idempotency_key)
            if existing:
                return existing
            raise

    async def update_operation(
        self,
        idempotency_key: str,
        status: str,
        *,
        item_id: str | None = None,
        error: str | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> Mapping[str, Any]:
        sql_status = {
            "completed": "succeeded",
            "calendar_created": "pending",
            "sync_failed": "failed",
        }.get(status, status)
        changes: dict[str, Any] = {
            "status": sql_status,
            "error_message": error,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if item_id is not None:
            changes["item_id"] = item_id
        if payload is not None:
            changes["payload"] = dict(payload)

        def execute() -> Any:
            return (
                self._client.table("operations")
                .update(changes)
                .eq("idempotency_key", idempotency_key)
                .execute()
            )

        response = await self._run(execute)
        if not response.data:
            raise ItemNotFoundError(f"operation {idempotency_key}")
        return response.data[0]


# Backward-compatible name for callers that describe this object as an adapter.
SupabaseAdapter = SupabaseRepository
