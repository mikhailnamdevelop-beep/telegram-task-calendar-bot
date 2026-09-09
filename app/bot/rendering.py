"""Translate service responses into aiogram objects without business rules."""

from __future__ import annotations

from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .contracts import BotResponse, Button


def coerce_response(value: BotResponse | str | dict[str, Any] | Any) -> BotResponse:
    if isinstance(value, BotResponse):
        return value
    if isinstance(value, str):
        return BotResponse(text=value)
    if isinstance(value, dict):
        raw_buttons = value.get("buttons", ())
        return BotResponse(
            text=str(value.get("text", "")),
            buttons=tuple(_coerce_button(button) for button in raw_buttons),
            alert=bool(value.get("alert", False)),
        )
    # Supporting compatible domain response DTOs keeps the transport decoupled.
    return BotResponse(
        text=str(getattr(value, "text", value)),
        buttons=tuple(_coerce_button(button) for button in getattr(value, "buttons", ())),
        alert=bool(getattr(value, "alert", False)),
    )


def _coerce_button(value: Button | dict[str, str] | Any) -> Button:
    if isinstance(value, Button):
        return value
    if isinstance(value, dict):
        return Button(text=str(value["text"]), data=str(value["data"]))
    return Button(text=str(value.text), data=str(value.data))


def markup(response: BotResponse) -> InlineKeyboardMarkup | None:
    if not response.buttons:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=button.text, callback_data=button.data)]
            for button in response.buttons
        ]
    )
