from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import pytest

from app.domain import DateRange, Intent, Target
from app.parser import CommandParser, parse_command

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=ZoneInfo("Asia/Seoul"))  # Wednesday


def parse(text, now=NOW, timezone="Asia/Seoul"):
    return parse_command(text, now=now, timezone=timezone)


@pytest.mark.parametrize(
    "slash, target",
    [("task", Target.TASK), ("calendar", Target.CALENDAR), ("both", Target.BOTH)],
)
def test_create_slash(slash, target):
    command = parse(f"/{slash}@PlaceholderBot Купить молоко завтра в 15:00")
    assert command.intent == Intent.ADD
    assert command.target == target
    assert command.title == "Купить молоко"
    assert command.scheduled_date == date(2026, 9, 10)
    assert command.start_time == time(15)
    assert command.missing_fields == []


@pytest.mark.parametrize(
    "text, target",
    [
        ("добавь задачу Купить молоко завтра", Target.TASK),
        ("создай событие Звонок завтра в 15:00", Target.CALENDAR),
        ("запиши Звонок в календарь завтра в 15:00", Target.CALENDAR),
        ("добавь Звонок в задачи и в календарь завтра в 15:00", Target.BOTH),
        ("add task Buy milk tomorrow", Target.TASK),
        ("create event Call tomorrow at 3 pm", Target.CALENDAR),
        ("add Call to tasks and calendar tomorrow at 15:00", Target.BOTH),
    ],
)
def test_natural_intents_and_targets(text, target):
    command = parse(text)
    assert command.intent == Intent.ADD
    assert command.target == target
    assert command.missing_fields == []


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("сегодня", date(2026, 9, 9)),
        ("завтра", date(2026, 9, 10)),
        ("послезавтра", date(2026, 9, 11)),
        ("вчера", date(2026, 9, 8)),
        ("позавчера", date(2026, 9, 7)),
        ("today", date(2026, 9, 9)),
        ("tomorrow", date(2026, 9, 10)),
        ("day after tomorrow", date(2026, 9, 11)),
        ("yesterday", date(2026, 9, 8)),
        ("day before yesterday", date(2026, 9, 7)),
        ("через 3 дня", date(2026, 9, 12)),
        ("через две недели", date(2026, 9, 23)),
        ("через месяц", date(2026, 10, 9)),
        ("через 2 года", date(2028, 9, 9)),
        ("in 3 days", date(2026, 9, 12)),
        ("in two weeks", date(2026, 9, 23)),
        ("in a month", date(2026, 10, 9)),
        ("in 2 years", date(2028, 9, 9)),
        ("3 дня назад", date(2026, 9, 6)),
        ("2 недели назад", date(2026, 8, 26)),
        ("3 months ago", date(2026, 6, 9)),
        ("2 years ago", date(2024, 9, 9)),
    ],
)
def test_relative_dates(phrase, expected):
    command = parse(f"/task Проверить {phrase}")
    assert command.scheduled_date == expected
    assert command.title == "Проверить"
    assert not command.missing_fields


@pytest.mark.parametrize(
    "phrase, now, expected",
    [
        (
            "через месяц",
            datetime(2026, 1, 31, tzinfo=ZoneInfo("Asia/Seoul")),
            date(2026, 2, 28),
        ),
        (
            "in one year",
            datetime(2024, 2, 29, tzinfo=ZoneInfo("Asia/Seoul")),
            date(2025, 2, 28),
        ),
        (
            "месяц назад",
            datetime(2026, 3, 31, tzinfo=ZoneInfo("Asia/Seoul")),
            date(2026, 2, 28),
        ),
        (
            "1 месяц назад",
            datetime(2026, 3, 31, tzinfo=ZoneInfo("Asia/Seoul")),
            date(2026, 2, 28),
        ),
    ],
)
def test_calendar_arithmetic(phrase, now, expected):
    assert parse(f"/task Проверить {phrase}", now).scheduled_date == expected


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("в эту пятницу", date(2026, 9, 11)),
        ("в этот понедельник", date(2026, 9, 7)),
        ("в ближайшую среду", date(2026, 9, 9)),
        ("в следующий понедельник", date(2026, 9, 14)),
        ("в следующую пятницу", date(2026, 9, 18)),
        ("this Friday", date(2026, 9, 11)),
        ("nearest Wednesday", date(2026, 9, 9)),
        ("next Friday", date(2026, 9, 18)),
        ("on Monday", date(2026, 9, 14)),
    ],
)
def test_weekdays(phrase, expected):
    assert parse(f"/task Проверить {phrase}").scheduled_date == expected


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("2027-01-15", date(2027, 1, 15)),
        ("15.01.2027", date(2027, 1, 15)),
        ("15 января 2027", date(2027, 1, 15)),
        ("в 15 сентября 2026", date(2026, 9, 15)),
        ("15 January 2027", date(2027, 1, 15)),
        ("January 15, 2027", date(2027, 1, 15)),
        ("January 15th, 2027", date(2027, 1, 15)),
        ("29 февраля 2028", date(2028, 2, 29)),
    ],
)
def test_absolute_dates(phrase, expected):
    command = parse(f"/task Проверить {phrase}")
    assert command.scheduled_date == expected
    assert command.title == "Проверить"


@pytest.mark.parametrize(
    "phrase",
    [
        "31.02.2026",
        "2026-02-29",
        "32 января 2027",
    ],
)
def test_invalid_or_incomplete_dates_are_not_guessed(phrase):
    command = parse(f"/task Проверить {phrase}")
    assert command.scheduled_date is None
    assert "scheduled_date" in command.missing_fields
    assert command.ambiguities


@pytest.mark.parametrize("phrase", ["15 сентября", "September 15", "15.09"])
def test_dates_without_year_use_current_year(phrase):
    command = parse(f"/task Проверить {phrase}")
    assert command.scheduled_date == date(2026, 9, 15)
    assert "scheduled_date" not in command.missing_fields


@pytest.mark.parametrize(
    "phrase, expected",
    [
        ("в 15:00", time(15)),
        ("15:30", time(15, 30)),
        ("at 3 pm", time(15)),
        ("at 3:45 pm", time(15, 45)),
        ("12 am", time(0)),
        ("12 pm", time(12)),
        ("в три часа дня", time(15)),
        ("в 3 часа дня", time(15)),
        ("в девять утра", time(9)),
        ("в двадцать один", time(21)),
        ("в полдень", time(12)),
        ("в полночь", time(0)),
        ("noon", time(12)),
        ("at 17", time(17)),
    ],
)
def test_time_expressions(phrase, expected):
    command = parse(f"/calendar Звонок завтра {phrase}")
    assert command.start_time == expected
    assert command.title == "Звонок"
    assert command.missing_fields == []


@pytest.mark.parametrize(
    "phrase", ["в три", "at 3", "вечером", "в 25:00", "в 15:90", "15 pm", "в 0 вечера"]
)
def test_ambiguous_or_invalid_times_require_clarification(phrase):
    command = parse(f"/calendar Звонок завтра {phrase}")
    assert command.start_time is None
    assert "start_time" in command.missing_fields
    assert command.ambiguities


@pytest.mark.parametrize(
    "phrase, duration",
    [
        ("с 15:00 до 16:30", 90),
        ("15:00-16:00", 60),
        ("from 3 pm to 4 pm", 60),
        ("в 15:00 на 2 часа", 120),
        ("в 15:00 на 1 час 30 минут", 90),
        ("at 15:00 for 30 minutes", 30),
        ("at 15:00 for 2 hours and 15 minutes", 135),
        ("в 15:00 на полтора часа", 90),
        ("в 15:00 на полчаса", 30),
    ],
)
def test_duration_and_intervals(phrase, duration):
    command = parse(f"/calendar Звонок завтра {phrase}")
    assert command.start_time == time(15)
    assert command.duration_minutes == duration
    assert command.missing_fields == []


@pytest.mark.parametrize(
    "phrase", ["с 23:00 до 01:00", "15:00-15:00", "15:00-16:00 на 90 минут"]
)
def test_ambiguous_intervals_are_explicit(phrase):
    command = parse(f"/calendar Звонок завтра {phrase}")
    assert "duration_minutes" in command.missing_fields
    assert command.duration_minutes is None
    assert command.ambiguities


@pytest.mark.parametrize(
    "phrase, start, end",
    [
        ("сегодня", date(2026, 9, 9), date(2026, 9, 9)),
        ("завтра", date(2026, 9, 10), date(2026, 9, 10)),
        ("на эту неделю", date(2026, 9, 7), date(2026, 9, 13)),
        ("на этой неделе", date(2026, 9, 7), date(2026, 9, 13)),
        ("на следующей неделе", date(2026, 9, 14), date(2026, 9, 20)),
        ("next week", date(2026, 9, 14), date(2026, 9, 20)),
        ("за прошлую неделю", date(2026, 8, 31), date(2026, 9, 6)),
        ("на следующий месяц", date(2026, 10, 1), date(2026, 10, 31)),
        ("this month", date(2026, 9, 1), date(2026, 9, 30)),
        ("на следующий год", date(2027, 1, 1), date(2027, 12, 31)),
        ("next 7 days", date(2026, 9, 9), date(2026, 9, 15)),
        ("за последние 7 дней", date(2026, 9, 3), date(2026, 9, 9)),
        ("с 2026-09-10 по 2026-09-15", date(2026, 9, 10), date(2026, 9, 15)),
        (
            "from September 10, 2026 to September 15, 2026",
            date(2026, 9, 10),
            date(2026, 9, 15),
        ),
        ("с завтра до послезавтра", date(2026, 9, 10), date(2026, 9, 11)),
    ],
)
def test_agenda_ranges(phrase, start, end):
    command = parse(f"/agenda {phrase}")
    assert command.intent == Intent.AGENDA
    assert command.date_range == DateRange(start=start, end=end)
    assert not command.missing_fields


@pytest.mark.parametrize(
    "text, intent, reference",
    [
        ("/delete #abcd1234", Intent.DELETE, "abcd1234"),
        ("/done #abcd1234", Intent.DONE, "abcd1234"),
        ("удали Купить молоко", Intent.DELETE, "Купить молоко"),
        ('complete "Buy milk"', Intent.DONE, "Buy milk"),
        ("/edit #abcd1234 завтра в 15:00", Intent.EDIT, "abcd1234"),
        ('перенеси "Звонок завтра" послезавтра в 15:00', Intent.EDIT, "Звонок завтра"),
    ],
)
def test_mutation_references(text, intent, reference):
    command = parse(text)
    assert command.intent == intent
    assert command.item_reference == reference
    assert not command.missing_fields


def test_rename_with_reference():
    command = parse('/edit #abcd1234 название "Новый заголовок"')
    assert command.item_reference == "abcd1234"
    assert command.title == "Новый заголовок"
    assert not command.missing_fields


def test_quoted_title_is_not_parsed_as_instructions_or_dates():
    command = parse('/task "Купить календарь завтра в 3 pm" 2026-09-15')
    assert command.title == "Купить календарь завтра в 3 pm"
    assert command.scheduled_date == date(2026, 9, 15)
    assert command.start_time is None


@pytest.mark.parametrize("target", ["calendar", "both"])
def test_complete_calendar_proposal_has_visible_default_duration(target):
    command = parse(f"/{target} Звонок завтра 15:00")
    assert command.duration_minutes == 60
    assert "duration.default_60_minutes" in command.warnings
    assert not command.needs_clarification


def test_task_and_incomplete_calendar_have_no_default_duration():
    assert parse("/task Купить молоко завтра").duration_minutes is None
    assert parse("/calendar Звонок завтра").duration_minutes is None


def test_configurable_default_duration():
    command = CommandParser(default_duration_minutes=30).parse(
        "/calendar Звонок завтра 15:00", NOW
    )
    assert command.duration_minutes == 30
    assert "duration.default_30_minutes" in command.warnings


def test_missing_fields_and_raw_text():
    text = "Купить молоко"
    command = parse(text)
    assert command.raw_text == text
    assert command.missing_fields == ["target", "scheduled_date"]
    assert command.needs_clarification
    assert parse("/calendar завтра").missing_fields == ["title", "start_time"]
    assert parse("/edit #abcd1234").missing_fields == ["changes"]
    assert parse("/done").missing_fields == ["item_reference"]
    assert parse("/agenda").missing_fields == ["date_range"]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("/task Купить молоко завтра послезавтра", "date.multiple_values"),
        ("/calendar Звонок завтра 15:00 16:00", "time.multiple_values"),
        ("/task Звонок в календарь завтра", "target.conflicting_destinations"),
        ("/delete #abcd #bcde", "item_reference.multiple"),
        ("/agenda с 2026-09-15 по 2026-09-10", "date_range.reversed"),
        ("/agenda this week tomorrow", "date_range.multiple_values"),
    ],
)
def test_conflicting_information(text, expected):
    command = parse(text)
    assert expected in command.ambiguities
    assert command.needs_clarification


def test_timezone_changes_relative_date():
    instant = datetime(2026, 9, 9, 18, tzinfo=ZoneInfo("UTC"))
    assert parse("/task Work today", instant, "Asia/Seoul").scheduled_date == date(
        2026, 9, 10
    )
    assert parse("/task Work today", instant, "UTC").scheduled_date == date(2026, 9, 9)


@pytest.mark.parametrize(
    "day, error",
    [
        ("2026-03-08", "time.nonexistent_local_time"),
        ("2026-11-01", "time.ambiguous_dst_fold"),
    ],
)
def test_dst_requires_clarification(day, error):
    hour = "02:30" if "03-08" in day else "01:30"
    command = parse(f"/calendar Call {day} {hour}", timezone="America/New_York")
    assert error in command.ambiguities
    assert command.start_time is None


def test_naive_clock_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        parse("/task Work today", now=datetime(2026, 9, 9))  # noqa: DTZ001 - intentionally invalid input


def test_parser_instance_and_function_agree():
    text = "/both Позвонить завтра в 15:00 на 30 минут"
    assert CommandParser("Asia/Seoul").parse(text, NOW) == parse(text)


def test_dateparser_receives_only_complete_extracted_fragments(monkeypatch):
    from app.parser import command_parser

    seen = []
    original = command_parser.dateparser.parse

    def wrapped(fragment, *args, **kwargs):
        seen.append(fragment)
        return original(fragment, *args, **kwargs)

    monkeypatch.setattr(command_parser.dateparser, "parse", wrapped)
    command = parse("/task Проверить отчет 15 сентября 2026 в 15:00")
    assert command.scheduled_date == date(2026, 9, 15)
    assert seen == ["15 сентября 2026"]


def test_unknown_command_and_empty_input():
    assert parse("/unknown tomorrow").intent == Intent.UNKNOWN
    assert parse("").missing_fields == ["intent"]
    assert parse("/task " + "x" * 4096).ambiguities == ["text.too_long"]
