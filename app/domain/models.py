"""Validated, transport-independent records shared by bot and storage adapters."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class Intent(str, Enum):
    ADD = "add"
    CREATE = ADD
    AGENDA = "agenda"
    EDIT = "edit"
    DELETE = "delete"
    DONE = "done"
    UNKNOWN = "unknown"


class Target(str, Enum):
    TASK = "task"
    CALENDAR = "calendar"
    BOTH = "both"


ItemType = Target


class ItemStatus(str, Enum):
    ACTIVE = "active"
    DONE = "done"
    CANCELLED = "cancelled"
    SYNC_PENDING = "sync_pending"
    CALENDAR_CREATED = "calendar_created"
    SYNC_FAILED = "sync_failed"


class DateRange(BaseModel):
    """A closed range: both start and end are included."""

    model_config = ConfigDict(extra="forbid")
    start: date
    end: date

    @model_validator(mode="after")
    def chronological(self) -> DateRange:
        if self.end < self.start:
            raise ValueError("date_range.end must not precede start")
        return self


class ParsedCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: Intent = Intent.UNKNOWN
    target: Target | None = None
    title: str | None = None
    item_reference: str | None = None
    scheduled_date: date | None = None
    start_time: time | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=10080)
    date_range: DateRange | None = None
    missing_fields: list[str] = Field(default_factory=list)
    raw_text: str = ""
    ambiguities: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def needs_clarification(self) -> bool:
        return bool(self.missing_fields or self.ambiguities)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Item(BaseModel):
    model_config = ConfigDict(
        extra="forbid", populate_by_name=True, validate_assignment=True
    )

    id: UUID = Field(default_factory=uuid4)
    short_id: str = Field(
        default_factory=lambda: uuid4().hex[:8], pattern=r"^[A-Za-z0-9_-]{4,32}$"
    )
    telegram_user_id: int = Field(gt=0)
    telegram_chat_id: int
    telegram_message_id: int | None = Field(default=None, gt=0)
    type: ItemType = Field(
        validation_alias=AliasChoices("type", "item_type"),
        serialization_alias="item_type",
    )
    title: str = Field(min_length=1, max_length=500)
    scheduled_date: date
    start_time: time | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=10080)
    timezone: str = "Asia/Seoul"
    status: ItemStatus = ItemStatus.ACTIVE
    google_event_id: str | None = None
    google_event_html_link: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "google_event_html_link", "google_event_link", "html_link"
        ),
    )
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_utc_now)
    updated_at: datetime = Field(default_factory=_utc_now)
    deleted_at: datetime | None = None

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("title must not be blank")
        return value

    @field_validator("timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone must be an IANA timezone") from exc
        return value

    @field_validator("created_at", "updated_at", "deleted_at")
    @classmethod
    def aware_datetime(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.utcoffset() is None:
            raise ValueError("timestamps must include a timezone")
        return value

    @property
    def item_type(self) -> ItemType:
        return self.type

    @property
    def html_link(self) -> str | None:
        return self.google_event_html_link
