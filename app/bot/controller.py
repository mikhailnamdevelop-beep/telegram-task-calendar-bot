"""Transport adapter between Telegram updates and the application service."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.domain import Intent, ParsedCommand, Target
from app.parser import CommandParser
from app.services import AmbiguousItemError

from .contracts import BotResponse, Button

HELP_TEXT = (
    "Команды:\n"
    "/task <дата> <задача> — только Supabase\n"
    "/calendar <дата> <время> <событие> — Google Calendar\n"
    "/both <дата> <время> <задача> — оба места\n"
    "/agenda [дата/период] — план\n"
    "/edit <ID> <изменение> · /delete <ID> · /done <ID>\n"
    "/today · /tomorrow · /week · /cancel\n\n"
    "Обычный текст создаёт задачу. Фразу «добавь в календарь» можно писать без /."
)


class TelegramController:
    """Keeps handler functions declarative and has no database knowledge."""

    def __init__(self, service: Any, parser: CommandParser) -> None:
        self.service = service
        self.parser = parser

    async def handle_command(
        self, *, user_id: int, chat_id: int, message_id: int, command: str, text: str
    ) -> BotResponse:
        if command in {"start", "help"}:
            return BotResponse(HELP_TEXT)
        if command == "cancel":
            await self.service.clear_dialog(user_id)
            return BotResponse("Cancelled.")
        if command in {"today", "tomorrow", "week"}:
            return await self._agenda(user_id, command)
        if command == "agenda":
            return await self._agenda(user_id, text or "today")
        if command == "delete":
            return await self._delete(user_id, chat_id, message_id, text)
        if command == "done":
            return await self._done(user_id, text)
        if command == "edit":
            return await self._edit(user_id, chat_id, message_id, text)
        if command not in {"task", "calendar", "both"}:
            return BotResponse("Unknown command. Use /help.")
        if not text.strip():
            return BotResponse(f"Add text after /{command}.")
        return await self._parse_and_present(
            user_id, chat_id, message_id, f"/{command} {text}"
        )

    async def handle_text(
        self, *, user_id: int, chat_id: int, message_id: int, text: str
    ) -> BotResponse:
        dialog = await self.service.get_dialog(user_id)
        if dialog and dialog.get("action") == "clarify":
            # The parser remains the authority when an ambiguous date needs context.
            original = dialog["raw_text"]
            return await self._parse_and_present(
                user_id, chat_id, message_id, f"{original} {text}"
            )
        return await self._parse_and_present(
            user_id, chat_id, message_id, text, default_to_task=True
        )

    async def handle_callback(self, *, user_id: int, data: str) -> BotResponse:
        action, _, value = data.partition(":")
        if action == "cancel":
            await self.service.clear_dialog(user_id)
            return BotResponse("Cancelled.")
        if action == "confirm":
            dialog = await self.service.get_dialog(user_id)
            if not dialog or dialog.get("action") != "confirm":
                return BotResponse("This confirmation expired. Please send the request again.", alert=True)
            command = self._restore_command(dialog)
            operation = value or dialog.get("operation", "add")
            try:
                if operation == "add":
                    item = await self.service.add(
                        command,
                        user_id=user_id,
                        chat_id=dialog["chat_id"],
                        message_id=dialog["message_id"],
                    )
                elif operation == "edit":
                    item = await self.service.edit(command, user_id)
                elif operation == "delete":
                    scope = command.target.value if command.target else None
                    item = await self.service.delete(
                        command.item_reference, user_id, scope=scope
                    )
                else:
                    return BotResponse("This action is no longer available.", alert=True)
            except AmbiguousItemError as exc:
                return await self._present_candidates(
                    user_id, exc, operation, command
                )
            await self.service.clear_dialog(user_id)
            labels = {"add": "Saved", "edit": "Updated", "delete": "Deleted"}
            return self._item_response(
                labels[operation], item, undo=operation == "add"
            )
        if action == "undo" and value:
            item = await self.service.undo(value, user_id)
            return self._item_response("Undone", item)
        if action == "candidate" and value:
            return await self._choose_candidate(user_id, value)
        return BotResponse("This action is no longer available. Use /help.", alert=True)

    async def _parse_and_present(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        raw_text: str,
        *,
        default_to_task: bool = False,
    ) -> BotResponse:
        command = self.parser.parse(raw_text)
        if (
            default_to_task
            and command.intent == Intent.ADD
            and command.target is None
            and "target.conflicting_destinations" not in command.ambiguities
        ):
            command = self.parser.parse(f"/task {raw_text}")
        if command.missing_fields or command.ambiguities:
            await self.service.save_dialog(
                user_id,
                {
                    "action": "clarify",
                    "raw_text": raw_text,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            details = list(command.missing_fields) + list(command.ambiguities)
            return BotResponse("I need one detail: " + "; ".join(map(str, details)))
        await self.service.clear_dialog(user_id)
        if command.intent == Intent.AGENDA:
            return await self._agenda_command(user_id, command)
        if command.intent == Intent.EDIT:
            return await self._confirm_command(
                user_id, chat_id, message_id, "edit", command
            )
        if command.intent == Intent.DELETE:
            return await self._confirm_command(
                user_id, chat_id, message_id, "delete", command
            )
        if command.intent == Intent.DONE:
            return await self._execute_done(user_id, command.item_reference or "")
        if command.intent != Intent.ADD:
            return BotResponse("Unknown request. Use /help.")
        if (
            command.target in {Target.CALENDAR, Target.BOTH}
            or "date.in_past" in command.warnings
        ):
            return await self._confirm_command(
                user_id, chat_id, message_id, "add", command
            )
        item = await self.service.add(
            command, user_id=user_id, chat_id=chat_id, message_id=message_id
        )
        return self._item_response("Saved", item, undo=True)

    async def _confirm_command(
        self,
        user_id: int,
        chat_id: int,
        message_id: int,
        operation: str,
        command: ParsedCommand,
    ) -> BotResponse:
        await self.service.save_dialog(
            user_id,
            {
                "action": "confirm",
                "operation": operation,
                "command": command.model_dump(mode="json"),
                "chat_id": chat_id,
                "message_id": message_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return BotResponse(
            self._preview(command, operation),
            buttons=(
                Button("Confirm", f"confirm:{operation}"),
                Button("Cancel", "cancel"),
            ),
        )

    async def _agenda(self, user_id: int, period: str) -> BotResponse:
        command = self.parser.parse(f"/agenda {period}")
        if command.missing_fields or command.ambiguities:
            return BotResponse(
                "I need one detail: "
                + "; ".join(command.missing_fields + command.ambiguities)
            )
        return await self._agenda_command(user_id, command)

    async def _agenda_command(
        self, user_id: int, command: ParsedCommand
    ) -> BotResponse:
        items = await self.service.agenda(command, user_id)
        if not items:
            return BotResponse("Nothing scheduled.")
        return BotResponse("Agenda:\n" + "\n".join(self._item_line(item) for item in items))

    async def _edit(
        self, user_id: int, chat_id: int, message_id: int, text: str
    ) -> BotResponse:
        return await self._parse_and_present(
            user_id, chat_id, message_id, f"/edit {text}"
        )

    async def _delete(
        self, user_id: int, chat_id: int, message_id: int, text: str
    ) -> BotResponse:
        return await self._parse_and_present(
            user_id, chat_id, message_id, f"/delete {text}"
        )

    async def _done(self, user_id: int, text: str) -> BotResponse:
        command = self.parser.parse(f"/done {text}")
        if command.missing_fields or command.ambiguities:
            return BotResponse("Use /done <item id>.")
        return await self._execute_done(user_id, command.item_reference or "")

    async def _execute_done(self, user_id: int, ref: str) -> BotResponse:
        try:
            item = await self.service.done(ref, user_id)
        except AmbiguousItemError as exc:
            return await self._present_candidates(user_id, exc, "done")
        return self._item_response("Done", item, undo=True)

    async def _choose_candidate(self, user_id: int, item_id: str) -> BotResponse:
        dialog = await self.service.get_dialog(user_id)
        if not dialog or dialog.get("action") != "candidate":
            return BotResponse("This selection expired. Please repeat the command.", alert=True)
        action = dialog["operation"]
        if action == "delete":
            command = self._restore_command(dialog) if dialog.get("command") else None
            scope = command.target.value if command and command.target else None
            item = await self.service.delete(item_id, user_id, scope=scope)
        elif action == "done":
            item = await self.service.done(item_id, user_id)
        elif action == "edit":
            command = self._restore_command(dialog).model_copy(
                update={"item_reference": item_id}
            )
            item = await self.service.edit(command, user_id)
        else:
            return BotResponse("This action is no longer available.", alert=True)
        await self.service.clear_dialog(user_id)
        return self._item_response(action.capitalize(), item, undo=action == "done")

    async def _present_candidates(
        self,
        user_id: int,
        exc: AmbiguousItemError,
        operation: str,
        command: ParsedCommand | None = None,
    ) -> BotResponse:
        await self.service.save_dialog(
            user_id,
            {
                "action": "candidate",
                "operation": operation,
                "command": command.model_dump(mode="json") if command is not None else None,
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        # Candidate ids are safe, short identifiers only.
        buttons = tuple(
            Button(self._item_line(item), f"candidate:{self._item_id(item)}")
            for item in exc.candidates
        )
        return BotResponse("Choose an item:", buttons=buttons)

    @staticmethod
    def _restore_command(dialog: dict[str, Any]) -> Any:
        from app.domain import ParsedCommand

        return ParsedCommand.model_validate(dialog["command"])

    def _preview(self, command: ParsedCommand, operation: str) -> str:
        reference = f" [{command.item_reference}]" if command.item_reference else ""
        if operation == "delete":
            return f"Confirm delete{reference}?"
        if operation == "edit":
            changes: list[str] = []
            if command.title:
                changes.append(f'title -> "{command.title}"')
            if command.scheduled_date:
                changes.append(f"date -> {command.scheduled_date}")
            if command.start_time:
                changes.append(f"time -> {command.start_time.strftime('%H:%M')}")
            if command.duration_minutes:
                changes.append(f"duration -> {command.duration_minutes} min")
            return f"Confirm edit{reference}: {', '.join(changes)}?"

        title = command.title or "Untitled"
        target = getattr(command.target, "value", str(command.target))
        date = command.scheduled_date or "no date"
        clock = command.start_time.strftime("%H:%M") if command.start_time else "no time"
        duration = (
            f", {command.duration_minutes} min" if command.duration_minutes else ""
        )
        return f"Confirm {operation} {target}{reference}: {title} ({date}, {clock}{duration})?"

    def _item_response(self, label: str, item: Any, *, undo: bool = False) -> BotResponse:
        item_id = self._item_id(item)
        buttons = (Button("Undo", f"undo:{item_id}"),) if undo and item_id else ()
        return BotResponse(f"{label}: {self._item_line(item)}", buttons=buttons)

    @staticmethod
    def _item_id(item: Any) -> str:
        return str(getattr(item, "short_id", None) or getattr(item, "id", ""))

    @staticmethod
    def _item_line(item: Any) -> str:
        title = (getattr(item, "title", None) or str(item)).replace("\n", " ")
        item_id = TelegramController._item_id(item)
        scheduled_date = getattr(item, "scheduled_date", None)
        start_time = getattr(item, "start_time", None)
        when = ""
        if scheduled_date:
            when = f" {scheduled_date}"
            if start_time:
                when += f" {start_time.strftime('%H:%M')}"
        status = getattr(item, "status", None)
        status_value = getattr(status, "value", status)
        suffix = f" [{item_id}]" if item_id else ""
        if status_value:
            suffix += f" ({status_value})"
        return f"{when} {title}".strip() + suffix
