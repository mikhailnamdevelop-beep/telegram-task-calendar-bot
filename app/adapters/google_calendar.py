from __future__ import annotations

import asyncio
import random
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from typing import Any, TypeVar
from zoneinfo import ZoneInfo

from app.domain import Item

T = TypeVar("T")


class GoogleCalendarAdapter:
    """Small Calendar API adapter with retry and item-id deduplication."""

    SCOPES = ("https://www.googleapis.com/auth/calendar.events",)

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        refresh_token: str | None = None,
        calendar_id: str = "primary",
        timezone: str = "Asia/Seoul",
        *,
        service: Any | None = None,
        max_attempts: int = 4,
    ):
        if service is None:
            if not client_id or not client_secret or not refresh_token:
                raise ValueError(
                    "Google client_id, client_secret, and refresh_token are required"
                )
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build  # type: ignore[import-untyped]

            credentials = Credentials(
                token=None,
                refresh_token=refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=client_id,
                client_secret=client_secret,
                scopes=list(self.SCOPES),
            )
            service = build(
                "calendar", "v3", credentials=credentials, cache_discovery=False
            )
        self._service = service
        self.calendar_id = calendar_id
        self.timezone_name = timezone
        self.max_attempts = max(1, max_attempts)

    @classmethod
    def from_refresh_token(
        cls,
        *,
        client_id: str,
        client_secret: str,
        refresh_token: str,
        calendar_id: str = "primary",
        timezone_name: str = "Asia/Seoul",
    ) -> GoogleCalendarAdapter:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build  # type: ignore[import-untyped]

        credentials = Credentials(
            token=None,
            refresh_token=refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=client_id,
            client_secret=client_secret,
            scopes=list(cls.SCOPES),
        )
        service = build(
            "calendar", "v3", credentials=credentials, cache_discovery=False
        )
        return cls(service=service, calendar_id=calendar_id, timezone=timezone_name)

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        status = getattr(getattr(exc, "resp", None), "status", None)
        return status in {408, 429, 500, 502, 503, 504}

    async def _execute(self, request_factory: Callable[[], Any]) -> Any:
        for attempt in range(self.max_attempts):
            try:
                return await asyncio.to_thread(lambda: request_factory().execute())
            except Exception as exc:
                if attempt + 1 >= self.max_attempts or not self._retryable(exc):
                    raise
                delay = min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.2)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    def _event_body(self, item: Item) -> dict[str, Any]:
        zone = ZoneInfo(item.timezone or self.timezone_name)
        if item.start_time is None:
            start: dict[str, Any] = {"date": item.scheduled_date.isoformat()}
            end = {"date": (item.scheduled_date + timedelta(days=1)).isoformat()}
        else:
            starts_at = datetime.combine(item.scheduled_date, item.start_time, zone)
            ends_at = starts_at + timedelta(minutes=item.duration_minutes or 60)
            start = {"dateTime": starts_at.isoformat(), "timeZone": zone.key}
            end = {"dateTime": ends_at.isoformat(), "timeZone": zone.key}
        return {
            "summary": item.title,
            "start": start,
            "end": end,
            "extendedProperties": {"private": {"item_id": str(item.id)}},
        }

    async def find_event_by_item_id(self, item_id: str) -> Mapping[str, Any] | None:
        response = await self._execute(
            lambda: self._service.events().list(
                calendarId=self.calendar_id,
                privateExtendedProperty=f"item_id={item_id}",
                showDeleted=False,
                singleEvents=True,
                maxResults=2,
            )
        )
        events = response.get("items", [])
        return events[0] if events else None

    async def create_event(self, item: Item) -> Mapping[str, Any]:
        existing = await self.find_event_by_item_id(str(item.id))
        if existing:
            return existing
        body = self._event_body(item)
        for attempt in range(self.max_attempts):
            try:
                return await asyncio.to_thread(
                    lambda: (
                        self._service.events()
                        .insert(
                            calendarId=self.calendar_id,
                            body=body,
                            sendUpdates="none",
                        )
                        .execute()
                    )
                )
            except Exception as exc:
                if attempt + 1 >= self.max_attempts or not self._retryable(exc):
                    raise
                # The write may have reached Google even when its response was lost.
                # Look up the private item_id before issuing another insert.
                await asyncio.sleep(
                    min(8.0, 0.5 * (2**attempt)) + random.uniform(0, 0.2)
                )
                existing = await self.find_event_by_item_id(str(item.id))
                if existing:
                    return existing
        raise AssertionError("unreachable")

    async def update_event(self, event_id: str, item: Item) -> Mapping[str, Any]:
        return await self._execute(
            lambda: self._service.events().patch(
                calendarId=self.calendar_id,
                eventId=event_id,
                body=self._event_body(item),
                sendUpdates="none",
            )
        )

    async def delete_event(self, event_id: str) -> None:
        try:
            await self._execute(
                lambda: self._service.events().delete(
                    calendarId=self.calendar_id,
                    eventId=event_id,
                    sendUpdates="none",
                )
            )
        except Exception as exc:
            # Deleting an already absent event is idempotent.
            if getattr(getattr(exc, "resp", None), "status", None) != 404:
                raise

    async def list_events(
        self, start: datetime, end: datetime
    ) -> list[Mapping[str, Any]]:
        events: list[Mapping[str, Any]] = []
        page_token: str | None = None
        while True:
            current_page_token = page_token
            def request_factory(token: str | None = current_page_token) -> Any:
                return self._service.events().list(
                    calendarId=self.calendar_id,
                    timeMin=start.isoformat(),
                    timeMax=end.isoformat(),
                    singleEvents=True,
                    orderBy="startTime",
                    showDeleted=False,
                    maxResults=2500,
                    pageToken=token,
                )

            response = await self._execute(request_factory)
            events.extend(response.get("items", []))
            page_token = response.get("nextPageToken")
            if not page_token:
                return events
