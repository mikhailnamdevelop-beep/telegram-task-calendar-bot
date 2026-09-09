from __future__ import annotations

from app.bot.contracts import BotResponse, Button
from app.bot.rendering import coerce_response, markup


def test_dict_response_is_rendered_as_inline_buttons() -> None:
    response = coerce_response(
        {"text": "Choose", "buttons": [{"text": "Confirm", "data": "confirm"}]}
    )

    keyboard = markup(response)

    assert response.text == "Choose"
    assert keyboard is not None
    assert keyboard.inline_keyboard[0][0].callback_data == "confirm"


def test_response_without_buttons_has_no_markup() -> None:
    assert markup(BotResponse("Done", buttons=(Button("x", "x"),))) is not None
    assert markup(BotResponse("Done")) is None
