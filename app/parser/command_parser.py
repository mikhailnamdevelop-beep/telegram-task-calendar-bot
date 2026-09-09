"""Conservative RU/EN command parser with explicit ambiguity reporting.

Only recognized date fragments reach dateparser. Relative month/year arithmetic
uses relativedelta; injected `now` makes parsing reproducible. Date ranges include
both endpoints. Unqualified weekdays mean the nearest day, including today, and
that interpretation is returned as a warning. Dates without a year require a
clarification. Complete calendar proposals use a documented 60-minute duration
when none was supplied, and return a warning that the default was applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import dateparser
from dateutil.relativedelta import relativedelta

from app.domain import DateRange, Intent, ParsedCommand, Target

_NUMBER_WORDS = {
    "ноль": 0,
    "один": 1,
    "одну": 1,
    "одна": 1,
    "два": 2,
    "две": 2,
    "три": 3,
    "четыре": 4,
    "пять": 5,
    "шесть": 6,
    "семь": 7,
    "восемь": 8,
    "девять": 9,
    "десять": 10,
    "одиннадцать": 11,
    "двенадцать": 12,
    "тринадцать": 13,
    "четырнадцать": 14,
    "пятнадцать": 15,
    "шестнадцать": 16,
    "семнадцать": 17,
    "восемнадцать": 18,
    "девятнадцать": 19,
    "двадцать": 20,
    "двадцать один": 21,
    "двадцать два": 22,
    "двадцать три": 23,
    "тридцать": 30,
    "сорок": 40,
    "сорок пять": 45,
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "eleven": 11,
    "twelve": 12,
}
_NUMBER = r"(?:\d{1,6}|" + "|".join(sorted(_NUMBER_WORDS, key=len, reverse=True)) + r")"
_UNIT = r"(?:дней|дня|день|недел[яьиью]|недели|месяц(?:а|ев)?|год(?:а|ов)?|лет|days?|weeks?|months?|years?)"
_MONTH = r"(?:январ[ьяь]|феврал[ьяь]|март[а]?|апрел[ьяь]|ма[йя]|июн[ьяь]|июл[ьяь]|август[а]?|сентябр[ьяь]|октябр[ьяь]|ноябр[ьяь]|декабр[ьяь]|january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec)"
_WEEKDAYS = {
    "понедельник": 0,
    "понедельника": 0,
    "monday": 0,
    "вторник": 1,
    "вторника": 1,
    "tuesday": 1,
    "среда": 2,
    "среду": 2,
    "среды": 2,
    "wednesday": 2,
    "четверг": 3,
    "четверга": 3,
    "thursday": 3,
    "пятница": 4,
    "пятницу": 4,
    "пятницы": 4,
    "friday": 4,
    "суббота": 5,
    "субботу": 5,
    "субботы": 5,
    "saturday": 5,
    "воскресенье": 6,
    "воскресенья": 6,
    "sunday": 6,
}
_WEEKDAY = "(?:" + "|".join(sorted(_WEEKDAYS, key=len, reverse=True)) + ")"
_DAYPART = r"(?:утра|утром|дня|днем|вечера|вечером|ночи|ночью|am|pm|a\.m\.|p\.m\.)"
_CLOCK = rf"(?:\d{{1,2}}(?::\d{{2}})?(?:\s*{_DAYPART})?)"
_QUOTED = re.compile(r'"[^"\n]+"|«[^»\n]+»|“[^”\n]+”')


def _number(value: str | None) -> int:
    if value is None:
        return 1
    return int(value) if value.isdigit() else _NUMBER_WORDS[value]


@dataclass
class _Text:
    original: str

    def __post_init__(self) -> None:
        self.value = self.original.lower().replace("ё", "е")
        self.used: list[tuple[int, int]] = []
        self.quoted = [
            (match.start(), match.end()) for match in _QUOTED.finditer(self.value)
        ]

    def matches(self, pattern: str) -> list[re.Match[str]]:
        return [
            match
            for match in re.finditer(pattern, self.value, re.IGNORECASE)
            if not any(
                match.start() < end and match.end() > start
                for start, end in self.quoted
            )
        ]

    def consume(self, match: re.Match[str]) -> None:
        self.remove(match.start(), match.end())

    def remove(self, start: int, end: int) -> None:
        self.used.append((start, end))
        self.value = self.value[:start] + " " * (end - start) + self.value[end:]

    def rest(self) -> str:
        chars = list(self.original)
        for start, end in self.used:
            chars[start:end] = " " * (end - start)
        return re.sub(r"\s+", " ", "".join(chars)).strip(" \t\n,;:.-—")


class CommandParser:
    def __init__(
        self, timezone: str = "Asia/Seoul", default_duration_minutes: int = 60
    ) -> None:
        self.timezone = timezone
        self.zone = ZoneInfo(timezone)
        if (
            isinstance(default_duration_minutes, bool)
            or not isinstance(default_duration_minutes, int)
            or not 1 <= default_duration_minutes <= 10080
        ):
            raise ValueError("default_duration_minutes must be between 1 and 10080")
        self.default_duration_minutes = default_duration_minutes

    def parse(self, text: str, now: datetime | None = None) -> ParsedCommand:
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        current = now or datetime.now(self.zone)
        if current.utcoffset() is None:
            raise ValueError("now must be timezone-aware")
        current = current.astimezone(self.zone)
        command = ParsedCommand(raw_text=text)
        if len(text) > 4096:
            command.ambiguities.append("text.too_long")
            command.missing_fields.append("intent")
            return command
        fragment = _Text(text)
        self._intent_and_target(fragment, command)
        self._reference(fragment, command)
        self._duration(fragment, command)
        self._range(fragment, command, current)
        self._dates(fragment, command, current)
        self._time(fragment, command)
        self._title(fragment, command)
        self._validate(command, current)
        command.missing_fields = list(dict.fromkeys(command.missing_fields))
        command.ambiguities = list(dict.fromkeys(command.ambiguities))
        command.warnings = list(dict.fromkeys(command.warnings))
        return command

    def _intent_and_target(self, fragment: _Text, command: ParsedCommand) -> None:
        slash = re.match(r"^\s*/([a-z]+)(?:@[a-z0-9_]+)?\b", fragment.value)
        if slash:
            action = slash.group(1)
            mapping = {
                "task": (Intent.ADD, Target.TASK),
                "calendar": (Intent.ADD, Target.CALENDAR),
                "both": (Intent.ADD, Target.BOTH),
                "agenda": (Intent.AGENDA, Target.BOTH),
                "edit": (Intent.EDIT, None),
                "delete": (Intent.DELETE, None),
                "done": (Intent.DONE, None),
            }
            if action not in mapping:
                command.ambiguities.append("intent.unknown_command")
            else:
                command.intent, command.target = mapping[action]
            fragment.consume(slash)
        else:
            for pattern, intent in (
                (
                    r"^\s*(?:покажи|показать|список|расписание|что\s+(?:у\s+меня|запланировано)|какие(?:\s+у\s+меня)?|show|list|agenda|schedule|what(?:'s| is))\b",
                    Intent.AGENDA,
                ),
                (
                    r"^\s*(?:измени|изменить|отредактируй|редактировать|перенеси|перенести|edit|change|move|reschedule)\b",
                    Intent.EDIT,
                ),
                (
                    r"^\s*(?:удали|удалить|отмени|отменить|delete|remove|cancel)\b",
                    Intent.DELETE,
                ),
                (
                    r"^\s*(?:заверши|завершить|выполнено|готово|сделано|отметь\s+(?:выполненным|выполненной|готовым)|done|complete|finish)\b",
                    Intent.DONE,
                ),
                (
                    r"^\s*(?:добавь|добавить|создай|создать|запиши|записать|запланируй|запланировать|напомни|add|create|plan|remind\s+me)\b",
                    Intent.ADD,
                ),
            ):
                matches = fragment.matches(pattern)
                if matches:
                    command.intent = intent
                    fragment.consume(matches[0])
                    break

        # Explicit destinations are commands, whereas nouns inside titles are data.
        both_patterns = (
            r"\b(?:и\s+в\s+задач[иу]\s*,?\s*и\s+в\s+календарь|в\s+задач[иу]\s+и\s+(?:в\s+)?календарь|в\s+календарь\s+и\s+(?:в\s+)?задач[иу])\b",
            r"\b(?:both|(?:to\s+)?tasks?\s+and\s+(?:to\s+)?calendar|(?:to\s+)?calendar\s+and\s+(?:to\s+)?tasks?)\b",
        )
        explicit: list[Target] = []
        for pattern in both_patterns:
            for match in fragment.matches(pattern):
                explicit.append(Target.BOTH)
                fragment.consume(match)
        for pattern, target in (
            (r"\b(?:в\s+календарь|to\s+(?:the\s+)?calendar)\b", Target.CALENDAR),
            (r"\b(?:в\s+задач[иу]|to\s+(?:the\s+)?tasks?)\b", Target.TASK),
            (r"^\s*(?:задач[аиу]|tasks?)\b", Target.TASK),
            (
                r"^\s*(?:событи[ея]|встреч[ауи]|календарь|events?|meetings?|calendar)\b",
                Target.CALENDAR,
            ),
        ):
            for match in fragment.matches(pattern):
                explicit.append(target)
                fragment.consume(match)
        if explicit:
            targets = set(explicit)
            if Target.BOTH in targets or targets == {Target.TASK, Target.CALENDAR}:
                selected = Target.BOTH
            else:
                selected = explicit[0]
            if (
                command.target is not None
                and command.intent == Intent.ADD
                and command.target != selected
            ):
                command.ambiguities.append("target.conflicting_destinations")
                command.missing_fields.append("target")
                command.target = None
            else:
                command.target = selected
            if command.intent == Intent.UNKNOWN and not slash:
                command.intent = Intent.ADD
        if command.intent == Intent.AGENDA and command.target is None:
            command.target = Target.BOTH
        # An action with timing is a create request; destination still needs a choice.
        if command.intent == Intent.UNKNOWN and not slash and fragment.value.strip():
            command.intent = Intent.ADD

    def _reference(self, fragment: _Text, command: ParsedCommand) -> None:
        if command.intent not in {Intent.EDIT, Intent.DELETE, Intent.DONE}:
            return
        refs = fragment.matches(r"(?<!\w)#([a-z0-9][a-z0-9_-]{0,63})\b")
        if len(refs) > 1:
            command.ambiguities.append("item_reference.multiple")
            command.missing_fields.append("item_reference")
        elif refs:
            command.item_reference = refs[0].group(1)
        for match in refs:
            fragment.consume(match)
        if not refs:
            quoted = _QUOTED.search(fragment.value)
            if quoted:
                command.item_reference = fragment.original[
                    quoted.start() + 1 : quoted.end() - 1
                ]
                fragment.consume(quoted)
            else:
                identifier = re.match(
                    r"^\s*((?:[tcb]-)?(?:[a-f0-9]{8}|[a-f0-9]{8}-[a-f0-9-]{27,}))\b",
                    fragment.value,
                )
                if identifier:
                    command.item_reference = fragment.original[
                        identifier.start(1) : identifier.end(1)
                    ]
                    fragment.consume(identifier)

    def _duration(self, fragment: _Text, command: ParsedCommand) -> None:
        durations: list[int] = []
        patterns = (
            (
                rf"\b(?:на|for|длительностью|duration(?:\s*:)?)\s+(?P<h>{_NUMBER})\s*(?:час(?:а|ов)?|hours?|hrs?|h)(?:\s*(?:и|and)?\s*(?P<m>{_NUMBER})\s*(?:минут(?:а|ы)?|minutes?|mins?|m))?\b",
                "hours",
            ),
            (
                rf"\b(?:на|for|длительностью|duration(?:\s*:)?)\s+(?P<m>{_NUMBER})\s*(?:минут(?:а|ы)?|minutes?|mins?|m)\b",
                "minutes",
            ),
            (
                r"\b(?:на|for)\s+(?:полтора\s+часа|an?\s+hour\s+and\s+a\s+half)\b",
                "ninety",
            ),
            (r"\b(?:на|for)\s+(?:полчаса|half\s+an?\s+hour)\b", "thirty"),
            (r"\b(?:на|for)\s+(?:час|an?\s+hour)\b", "sixty"),
        )
        for pattern, kind in patterns:
            for match in fragment.matches(pattern):
                if kind == "hours":
                    duration = _number(match.group("h")) * 60 + (
                        _number(match.group("m")) if match.group("m") else 0
                    )
                elif kind == "minutes":
                    duration = _number(match.group("m"))
                else:
                    duration = {"ninety": 90, "thirty": 30, "sixty": 60}[kind]
                durations.append(duration)
                fragment.consume(match)
        if len(set(durations)) > 1:
            command.ambiguities.append("duration.conflicting_values")
            command.missing_fields.append("duration_minutes")
        elif durations:
            if 0 < durations[0] <= 10080:
                command.duration_minutes = durations[0]
            else:
                command.ambiguities.append("duration.out_of_range")
                command.missing_fields.append("duration_minutes")

    @staticmethod
    def _clock(
        value: str, *, explicit_24h: bool = False
    ) -> tuple[time | None, str | None]:
        match = re.fullmatch(
            rf"\s*(?P<h>\d{{1,2}})(?::(?P<m>\d{{2}}))?\s*(?P<part>{_DAYPART})?\s*",
            value,
        )
        if not match:
            return None, "time.invalid"
        hour, minute = int(match.group("h")), int(match.group("m") or 0)
        part = match.group("part")
        if minute > 59 or hour > 23:
            return None, "time.invalid"
        if part:
            if not 1 <= hour <= 12:
                return None, "time.invalid_meridiem"
            if part in {"pm", "p.m.", "дня", "днем", "вечера", "вечером"}:
                hour = hour % 12 + 12
            else:
                hour %= 12
        elif not explicit_24h and match.group("m") is None and 1 <= hour <= 12:
            return None, "time.meridiem_required"
        return time(hour, minute), None

    def _time(self, fragment: _Text, command: ParsedCommand) -> None:
        found: list[time] = []
        interval_pattern = rf"(?<!\w)(?:(?:с|from)\s+)?(?P<start>{_CLOCK})\s*(?:[-–—]|до|to)\s*(?P<end>{_CLOCK})(?![\w.:\d-])"
        for match in fragment.matches(interval_pattern):
            # A bare number range may be an identifier/date, never infer a clock.
            if not any(
                marker in match.group(0)
                for marker in (
                    ":",
                    "am",
                    "pm",
                    "с ",
                    "from ",
                    "утра",
                    "дня",
                    "вечера",
                    "ночи",
                )
            ):
                continue
            start, start_error = self._clock(
                match.group("start"), explicit_24h=":" in match.group("start")
            )
            end, end_error = self._clock(
                match.group("end"), explicit_24h=":" in match.group("end")
            )
            errors = [error for error in (start_error, end_error) if error]
            if errors:
                command.ambiguities.extend(errors)
                command.missing_fields.append("start_time")
            elif start is not None and end is not None:
                found.append(start)
                minutes = end.hour * 60 + end.minute - start.hour * 60 - start.minute
                if minutes <= 0:
                    command.ambiguities.append("time.overnight_or_reversed_interval")
                    command.missing_fields.append("duration_minutes")
                elif (
                    command.duration_minutes is not None
                    and command.duration_minutes != minutes
                ):
                    command.ambiguities.append("duration.conflicting_interval")
                    command.missing_fields.append("duration_minutes")
                    command.duration_minutes = None
                else:
                    command.duration_minutes = minutes
            fragment.consume(match)

        for match in fragment.matches(
            rf"\bв\s+(?P<h>{_NUMBER})\s+час(?:а|ов)?(?:\s+(?P<part>{_DAYPART}))?\b"
        ):
            hour = _number(match.group("h"))
            part = match.group("part")
            parsed, error = self._clock(str(hour) + (" " + part if part else ""))
            if error:
                command.ambiguities.append(error)
                command.missing_fields.append("start_time")
            elif parsed is not None:
                found.append(parsed)
            fragment.consume(match)

        clock_pattern = rf"(?<![\w:.])(?:(?:в|at)\s+)?\d{{1,2}}:\d{{2}}(?:\s*{_DAYPART})?(?![\w:.])|(?<!\w)(?:(?:в|at)\s+)?\d{{1,2}}\s*{_DAYPART}(?!\w)|\b(?:в|at)\s+\d{{1,2}}(?![\w.:\d])(?:\s*{_DAYPART})?"
        for match in fragment.matches(clock_pattern):
            value = re.sub(r"^(?:в|at)\s+", "", match.group(0))
            parsed, error = self._clock(value, explicit_24h=":" in value)
            if error:
                command.ambiguities.append(error)
                command.missing_fields.append("start_time")
            elif parsed is not None:
                found.append(parsed)
            fragment.consume(match)

        for match in fragment.matches(
            rf"\bв\s+(?P<h>{_NUMBER})(?:\s+час(?:а|ов)?)?(?:\s+(?P<part>{_DAYPART}))?\b"
        ):
            hour = _number(match.group("h"))
            part = match.group("part")
            parsed, error = self._clock(str(hour) + (" " + part if part else ""))
            if error:
                command.ambiguities.append(error)
                command.missing_fields.append("start_time")
            elif parsed is not None:
                found.append(parsed)
            fragment.consume(match)
        for match in fragment.matches(
            r"\b(?:в\s+)?(?P<word>полдень|полночь|noon|midnight)\b"
        ):
            found.append(time(12 if match.group("word") in {"полдень", "noon"} else 0))
            fragment.consume(match)
        if len(set(found)) > 1:
            command.ambiguities.append("time.multiple_values")
            command.missing_fields.append("start_time")
        elif found and "start_time" not in command.missing_fields:
            command.start_time = found[0]
        for match in fragment.matches(
            r"\b(?:утром|днем|вечером|ночью|morning|afternoon|evening|tonight)\b"
        ):
            command.ambiguities.append("time.exact_time_required")
            command.missing_fields.append("start_time")
            fragment.consume(match)

    def _range(
        self, fragment: _Text, command: ParsedCommand, current: datetime
    ) -> None:
        if command.intent != Intent.AGENDA:
            return
        today = current.date()
        ranges: list[DateRange] = []
        for match in fragment.matches(
            r"\b(?:(?:на|за|в|for|during)\s+)?(?P<which>эт(?:а|у|ой)|текущ(?:ую|ая|ей)|следующ(?:ую|ая|ей)|прошл(?:ую|ая|ой)|this|next|last)\s+(?P<unit>недел[яюие]|week)\b"
        ):
            word = match.group("which")
            shift = (
                1
                if word.startswith("след") or word == "next"
                else -1
                if word.startswith("прош") or word == "last"
                else 0
            )
            start = today - timedelta(days=today.weekday()) + timedelta(weeks=shift)
            ranges.append(DateRange(start=start, end=start + timedelta(days=6)))
            fragment.consume(match)
        for match in fragment.matches(
            r"\b(?:(?:на|за|в|for|during)\s+)?(?P<which>этот|этом|текущий|текущем|следующий|следующем|прошлый|прошлом|this|next|last)\s+(?P<unit>месяц(?:е)?|год(?:у)?|month|year)\b"
        ):
            word, unit = match.group("which"), match.group("unit")
            shift = (
                1
                if word.startswith("след") or word == "next"
                else -1
                if word.startswith("прош") or word == "last"
                else 0
            )
            if unit.startswith("месяц") or unit == "month":
                start = today.replace(day=1) + relativedelta(months=shift)
                end = start + relativedelta(months=1) - timedelta(days=1)
            else:
                start = date(today.year + shift, 1, 1)
                end = date(start.year, 12, 31)
            ranges.append(DateRange(start=start, end=end))
            fragment.consume(match)
        for match in fragment.matches(
            rf"\b(?:(?:на|за|for|over|the)\s+)?(?P<direction>ближайшие|следующие|последние|прошедшие|next|past|last)\s+(?P<n>{_NUMBER})\s+(?P<unit>{_UNIT})\b"
        ):
            count, unit = _number(match.group("n")), self._unit(match.group("unit"))
            if count <= 0:
                command.ambiguities.append("date_range.invalid_length")
                command.missing_fields.append("date_range")
            else:
                try:
                    if match.group("direction") in {
                        "последние",
                        "прошедшие",
                        "past",
                        "last",
                    }:
                        start = (
                            today - self._relative_delta(unit, count) + timedelta(days=1)
                        )
                        ranges.append(DateRange(start=start, end=today))
                    else:
                        end = today + self._relative_delta(unit, count) - timedelta(days=1)
                        ranges.append(DateRange(start=today, end=end))
                    command.warnings.append("date_range.rolling_includes_today")
                except (OverflowError, ValueError):
                    command.ambiguities.append("date_range.out_of_range")
                    command.missing_fields.append("date_range")
            fragment.consume(match)
        if len(ranges) > 1:
            command.ambiguities.append("date_range.multiple_values")
            command.missing_fields.append("date_range")
        elif ranges:
            command.date_range = ranges[0]

    @staticmethod
    def _unit(value: str) -> str:
        if value.startswith(("д", "day")):
            return "days"
        if value.startswith(("нед", "week")):
            return "weeks"
        if value.startswith(("месяц", "month")):
            return "months"
        return "years"

    @staticmethod
    def _relative_delta(unit: str, count: int) -> relativedelta:
        if unit == "days":
            return relativedelta(days=count)
        if unit == "weeks":
            return relativedelta(weeks=count)
        if unit == "months":
            return relativedelta(months=count)
        return relativedelta(years=count)

    def _dates(
        self, fragment: _Text, command: ParsedCommand, current: datetime
    ) -> None:
        today = current.date()
        found: list[tuple[int, int, date]] = []

        def save(
            match: re.Match[str], result: date | None, error: str | None = None
        ) -> None:
            if result is not None:
                found.append((match.start(), match.end(), result))
            if error:
                command.ambiguities.append(error)
                command.missing_fields.append(
                    "date_range"
                    if command.intent == Intent.AGENDA
                    else "scheduled_date"
                )
            fragment.consume(match)

        patterns = (
            (r"(?<![\w\d])\d{4}-\d{2}-\d{2}(?![\w\d])", "%Y-%m-%d"),
            (r"(?<![\w\d])\d{1,2}\.\d{1,2}\.\d{4}(?![\w\d])", "%d.%m.%Y"),
        )
        for pattern, format_string in patterns:
            for match in fragment.matches(pattern):
                try:
                    result = (
                        datetime.strptime(match.group(0), format_string)
                        .replace(tzinfo=self.zone)
                        .date()
                    )
                except ValueError:
                    save(match, None, "date.invalid")
                else:
                    save(match, result)

        name_patterns = (
            rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\s*,?\s*\d{{4}}(?:\s*г(?:ода|\.)?)?\b",
            rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\s*,?\s*\d{{4}}\b",
        )
        for pattern in name_patterns:
            for match in fragment.matches(pattern):
                extracted = match.group(0)
                # Never submit the full user message or incomplete dates to dateparser.
                parsed = dateparser.parse(
                    extracted,
                    languages=["ru", "en"],
                    settings={
                        "STRICT_PARSING": True,
                        "DATE_ORDER": "DMY",
                        "PREFER_LOCALE_DATE_ORDER": False,
                        "RELATIVE_BASE": current.replace(tzinfo=None),
                        "TIMEZONE": self.timezone,
                        "RETURN_AS_TIMEZONE_AWARE": True,
                    },
                )
                save(
                    match,
                    parsed.date() if parsed else None,
                    None if parsed else "date.invalid",
                )

        for match in fragment.matches(
            rf"\b(?:через|in)\s+(?:(?P<n>{_NUMBER})\s+)?(?P<unit>{_UNIT})\b|\b(?:(?P<ago_n>{_NUMBER})\s+)?(?P<ago_unit>{_UNIT})\s+(?:назад|ago)\b"
        ):
            backward = match.group("ago_unit") is not None
            count = _number(match.group("ago_n") if backward else match.group("n"))
            unit = self._unit(
                match.group("ago_unit") if backward else match.group("unit")
            )
            try:
                result = today + self._relative_delta(
                    unit, -count if backward else count
                )
            except (OverflowError, ValueError):
                save(match, None, "date.out_of_range")
            else:
                save(match, result)

        relative_days = {
            "сегодня": 0,
            "завтра": 1,
            "послезавтра": 2,
            "вчера": -1,
            "позавчера": -2,
            "today": 0,
            "tomorrow": 1,
            "day after tomorrow": 2,
            "yesterday": -1,
            "day before yesterday": -2,
        }
        relative_pattern = (
            r"\b(?:" + "|".join(sorted(relative_days, key=len, reverse=True)) + r")\b"
        )
        for match in fragment.matches(relative_pattern):
            save(match, today + timedelta(days=relative_days[match.group(0)]))

        weekday_pattern = rf"\b(?:(?:в|на|on)\s+)?(?:(?P<modifier>этот|эту|это|этой|ближайший|ближайшую|ближайшее|следующий|следующую|следующее|this|nearest|next)\s+)?(?P<day>{_WEEKDAY})\b"
        for match in fragment.matches(weekday_pattern):
            weekday = _WEEKDAYS[match.group("day")]
            modifier = match.group("modifier") or ""
            if modifier.startswith("след") or modifier == "next":
                result = (
                    today
                    - timedelta(days=today.weekday())
                    + timedelta(days=7 + weekday)
                )
            elif modifier.startswith("эт") or modifier == "this":
                result = (
                    today - timedelta(days=today.weekday()) + timedelta(days=weekday)
                )
            else:
                result = today + timedelta(days=(weekday - today.weekday()) % 7)
                if not modifier:
                    command.warnings.append("date.weekday_nearest_including_today")
            save(match, result)

        for pattern in (
            rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH}\b",
            rf"\b{_MONTH}\s+\d{{1,2}}(?:st|nd|rd|th)?\b",
            r"(?<![\w\d.])\d{1,2}\.\d{1,2}(?![\w\d.])",
        ):
            for match in fragment.matches(pattern):
                extracted = match.group(0)
                try:
                    if re.fullmatch(r"\d{1,2}\.\d{1,2}", extracted):
                        candidate = f"{extracted}.{today.year}"
                        parsed = datetime.strptime(
                            candidate, "%d.%m.%Y"
                        ).replace(tzinfo=self.zone)
                    else:
                        parsed = dateparser.parse(
                            f"{extracted} {today.year}",
                            languages=["ru", "en"],
                            settings={
                                "STRICT_PARSING": True,
                                "DATE_ORDER": "DMY",
                                "PREFER_LOCALE_DATE_ORDER": False,
                                "RELATIVE_BASE": current.replace(tzinfo=None),
                            },
                        )
                except (OverflowError, ValueError):
                    parsed = None
                save(
                    match,
                    parsed.date() if parsed else None,
                    None if parsed else "date.invalid",
                )

        ordered = sorted(found)
        unique = list(dict.fromkeys(value for _, _, value in ordered))
        if len(ordered) == 2 and command.intent == Intent.AGENDA:
            between = fragment.original[ordered[0][1] : ordered[1][0]].lower()
            prefix = fragment.original[: ordered[0][0]].lower()
            is_range = bool(
                re.fullmatch(r"\s*(?:до|по|to|through|[-–—])\s*", between)
            ) and bool(
                re.search(r"\b(?:с|от|from)\s*$", prefix)
                or re.fullmatch(r"\s*[-–—]\s*", between)
            )
            if is_range:
                if command.date_range is not None:
                    command.ambiguities.append("date_range.multiple_values")
                    command.missing_fields.append("date_range")
                    command.date_range = None
                elif ordered[1][2] < ordered[0][2]:
                    command.ambiguities.append("date_range.reversed")
                    command.missing_fields.append("date_range")
                else:
                    command.date_range = DateRange(
                        start=ordered[0][2], end=ordered[1][2]
                    )
                return
        if len(unique) > 1:
            command.ambiguities.append("date.multiple_values")
            command.missing_fields.append(
                "date_range" if command.intent == Intent.AGENDA else "scheduled_date"
            )
        elif unique:
            if command.intent == Intent.AGENDA:
                if command.date_range is not None:
                    command.ambiguities.append("date_range.multiple_values")
                    command.missing_fields.append("date_range")
                    command.date_range = None
                elif "date_range" not in command.missing_fields:
                    command.date_range = DateRange(start=unique[0], end=unique[0])
                    command.scheduled_date = unique[0]
            elif "scheduled_date" not in command.missing_fields:
                command.scheduled_date = unique[0]

    def _title(self, fragment: _Text, command: ParsedCommand) -> None:
        remaining = fragment.rest()
        # Remove grammatical glue only at the edges after extracted fields.
        remaining = re.sub(
            r"^(?:(?:на|в|к|on|for|at|to)\s+)+", "", remaining, flags=re.IGNORECASE
        )
        remaining = re.sub(
            r"(?:\s+(?:на|в|к|on|for|at|to))+$", "", remaining, flags=re.IGNORECASE
        ).strip(" ,;:.-—")
        if command.intent == Intent.ADD:
            command.title = self._unquote(remaining) if remaining else None
        elif command.intent == Intent.EDIT:
            rename = re.search(
                r"(?:название(?:\s+на)?|назови|переименуй\s+в|title(?:\s+to)?|rename\s+to)\s*[:=]?\s*(.+)$",
                remaining,
                re.IGNORECASE,
            )
            if rename:
                command.title = self._unquote(rename.group(1).strip())
                if not command.item_reference:
                    reference = remaining[: rename.start()].strip(" ,;:-")
                    command.item_reference = self._unquote(reference) or None
            elif command.item_reference and remaining:
                edit_glue = r"(?:перенеси|перенести|измени|изменить|дата|время|длительность|move|reschedule|change|date|time|duration)(?:\s+(?:дату|время|date|time))?"
                if not re.fullmatch(edit_glue, remaining, re.IGNORECASE):
                    command.title = self._unquote(remaining)
            elif remaining:
                command.item_reference = self._unquote(remaining)
        elif (
            command.intent in {Intent.DELETE, Intent.DONE}
            and not command.item_reference
            and remaining
        ):
            command.item_reference = self._unquote(remaining)

    @staticmethod
    def _unquote(value: str) -> str:
        return (
            value[1:-1]
            if len(value) >= 2
            and (value[0], value[-1]) in {('"', '"'), ("«", "»"), ("“", "”")}
            else value
        )

    def _validate(self, command: ParsedCommand, current: datetime) -> None:
        if command.intent == Intent.UNKNOWN:
            command.missing_fields.append("intent")
            return
        if command.intent == Intent.ADD:
            for name in ("target", "title", "scheduled_date"):
                if getattr(command, name) is None:
                    command.missing_fields.append(name)
            if (
                command.target in {Target.CALENDAR, Target.BOTH}
                and command.start_time is None
            ):
                command.missing_fields.append("start_time")
        elif command.intent == Intent.AGENDA:
            if command.date_range is None:
                command.missing_fields.append("date_range")
        elif command.intent in {Intent.EDIT, Intent.DELETE, Intent.DONE}:
            if not command.item_reference:
                command.missing_fields.append("item_reference")
            if command.intent == Intent.EDIT and not any(
                (
                    command.title,
                    command.scheduled_date,
                    command.start_time,
                    command.duration_minutes,
                )
            ):
                command.missing_fields.append("changes")
        if command.title is not None and len(command.title) > 500:
            command.ambiguities.append("title.too_long")
            command.missing_fields.append("title")
        if (
            command.scheduled_date is not None
            and command.scheduled_date < current.date()
        ):
            command.warnings.append("date.in_past")
        if command.scheduled_date is not None and command.start_time is not None:
            local = datetime.combine(command.scheduled_date, command.start_time)
            candidates = [local.replace(tzinfo=self.zone, fold=fold) for fold in (0, 1)]
            valid = [
                candidate
                for candidate in candidates
                if candidate.astimezone(ZoneInfo("UTC"))
                .astimezone(self.zone)
                .replace(tzinfo=None)
                == local
            ]
            if not valid:
                command.ambiguities.append("time.nonexistent_local_time")
                command.missing_fields.append("start_time")
                command.start_time = None
            elif len({candidate.utcoffset() for candidate in valid}) > 1:
                command.ambiguities.append("time.ambiguous_dst_fold")
                command.missing_fields.append("start_time")
                command.start_time = None
        if (
            command.intent == Intent.ADD
            and command.target in {Target.CALENDAR, Target.BOTH}
            and not command.missing_fields
            and not command.ambiguities
            and command.duration_minutes is None
        ):
            command.duration_minutes = self.default_duration_minutes
            command.warnings.append(
                f"duration.default_{self.default_duration_minutes}_minutes"
            )


def parse_command(
    text: str,
    *,
    now: datetime | None = None,
    timezone: str = "Asia/Seoul",
    default_duration_minutes: int = 60,
) -> ParsedCommand:
    return CommandParser(
        timezone=timezone, default_duration_minutes=default_duration_minutes
    ).parse(text, now=now)
