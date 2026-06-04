"""Bot API 9.4 button style support for project-wide inline keyboards."""

from __future__ import annotations

from typing import Any, Literal, cast

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

ButtonStyle = Literal["primary", "success", "danger"]

_PATCHED_ATTR = "_botmother_button_styles_patched"

_DANGER_WORDS = (
    "delete",
    "remove",
    "revoke",
    "block",
    "ban",
    "cancel",
    "cleanup",
    "reset",
    "stop",
    "danger",
    "del:",
    "err_status:",
    "удал",
    "заблок",
    "забрать",
    "отмена",
    "очист",
    "сброс",
    "стоп",
)
_SUCCESS_WORDS = (
    "add",
    "create",
    "save",
    "start",
    "run",
    "confirm",
    "approve",
    "grant",
    "pay",
    "buy",
    "export",
    "import",
    "enable",
    "выдать",
    "запустить",
    "создать",
    "сохран",
    "подтверд",
    "экспорт",
    "импорт",
    "оплат",
    "купить",
    "вкл",
)
_PRIMARY_WORDS = (
    "main",
    "menu",
    "list",
    "select",
    "settings",
    "stats",
    "status",
    "next",
    "prev",
    "back",
    "refresh",
    "edit",
    "search",
    "раздел",
    "меню",
    "назад",
    "обнов",
    "настро",
    "стат",
    "поиск",
    "измен",
)


def infer_button_style(button: InlineKeyboardButton) -> ButtonStyle | None:
    """Infer Bot API 9.4 color style from text and callback semantics."""
    if button.url or button.web_app or button.login_url:
        return "primary"
    payload = " ".join(
        part
        for part in (button.text, button.callback_data or "")
        if isinstance(part, str)
    ).casefold()
    if not payload:
        return None
    if any(word in payload for word in _DANGER_WORDS):
        return "danger"
    if any(word in payload for word in _SUCCESS_WORDS):
        return "success"
    if any(word in payload for word in _PRIMARY_WORDS):
        return "primary"
    if (
        button.callback_data
        or button.switch_inline_query
        or button.switch_inline_query_current_chat
        or button.switch_inline_query_chosen_chat
        or getattr(button, "copy_text", None)
        or button.callback_game
        or button.pay
    ):
        return "primary"
    return None


def apply_button_styles(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    """Mutate markup with Bot API 9.4 style values while keeping aiogram 3.13 safe."""
    for row in markup.inline_keyboard:
        for button in row:
            extra = cast(dict[str, Any] | None, getattr(button, "__pydantic_extra__"))
            if extra and extra.get("style"):
                continue
            style = infer_button_style(button)
            if style:
                if extra is None:
                    extra = {}
                    setattr(button, "__pydantic_extra__", extra)
                extra["style"] = style
    return markup


def install_button_style_patch() -> None:
    """Patch aiogram keyboard serialization once for project-wide colored buttons."""
    if getattr(InlineKeyboardBuilder, _PATCHED_ATTR, False):
        return

    original_as_markup = InlineKeyboardBuilder.as_markup
    original_model_dump = InlineKeyboardMarkup.model_dump

    def styled_as_markup(
        self: InlineKeyboardBuilder,
        **kwargs: Any,
    ) -> InlineKeyboardMarkup:
        markup = original_as_markup(self, **kwargs)
        return apply_button_styles(markup)

    def styled_model_dump(
        self: InlineKeyboardMarkup,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        apply_button_styles(self)
        dumped = original_model_dump(self, *args, **kwargs)
        return cast(dict[str, Any], dumped)

    builder_cls = cast(Any, InlineKeyboardBuilder)
    markup_cls = cast(Any, InlineKeyboardMarkup)
    builder_cls.as_markup = styled_as_markup
    markup_cls.model_dump = styled_model_dump
    setattr(InlineKeyboardBuilder, _PATCHED_ATTR, True)

