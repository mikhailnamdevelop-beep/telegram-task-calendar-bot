from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.domain import DateRange, Intent, Item, ItemType, ParsedCommand, Target


def item_values():
    return {
        "telegram_user_id": 12345,
        "telegram_chat_id": 12345,
        "type": "task",
        "title": "Проверить",
        "scheduled_date": date(2026, 9, 9),
    }


def test_domain_enum_aliases_match_service_contract():
    assert Intent.CREATE is Intent.ADD
    assert Intent.ADD.value == "add"
    assert ItemType is Target


def test_item_json_roundtrip_uses_database_names():
    item = Item(**item_values())
    dumped = item.model_dump(mode="json", by_alias=True)
    assert dumped["item_type"] == "task"
    assert "type" not in dumped
    assert Item.model_validate(dumped) == item
    assert item.created_at.utcoffset() is not None
    assert len(item.short_id) == 8
    assert item.version == 1


def test_database_aliases_are_accepted():
    values = item_values()
    values["item_type"] = values.pop("type")
    values["html_link"] = "https://calendar.google.com/placeholder"
    item = Item(**values)
    assert item.item_type is Target.TASK
    assert item.google_event_html_link == values["html_link"]


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "  "},
        {"short_id": "a b c"},
        {"duration_minutes": 0},
        {"version": 0},
        {"timezone": "No/SuchZone"},
        {"created_at": datetime(2026, 9, 9)},  # noqa: DTZ001 - intentionally invalid input
    ],
)
def test_invalid_item_fields_rejected(changes):
    with pytest.raises(ValidationError):
        Item(**(item_values() | changes))


def test_reversed_date_range_rejected():
    with pytest.raises(ValidationError):
        DateRange(start=date(2026, 9, 10), end=date(2026, 9, 9))


def test_parser_lists_are_not_shared():
    first, second = ParsedCommand(), ParsedCommand()
    first.missing_fields.append("title")
    assert second.missing_fields == []
    assert first.needs_clarification


def test_aware_timestamp_accepted():
    item = Item(
        **(
            item_values()
            | {"updated_at": datetime(2026, 9, 9, tzinfo=ZoneInfo("Asia/Seoul"))}
        )
    )
    assert item.updated_at.utcoffset().total_seconds() == 9 * 3600
