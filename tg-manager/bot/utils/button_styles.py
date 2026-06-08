"""Bot API 9.4 button style support for project-wide inline keyboards."""

from __future__ import annotations

from typing import Any, Literal, cast

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder

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
    "отмена",
    "отменить",
    "удалить",
    "заблокировать",
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
    "добав",
    "добавить",
    "выбрать",
    "выбрать все",
    "применить",
    "продолжить",
    "готово",
    "проверить",
    "скан",
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
    "открыть",
    "реестр",
    "история",
    "лог",
    "след",
    "пред",
)


def infer_button_style(
    button: InlineKeyboardButton | KeyboardButton,
) -> ButtonStyle | None:
    """Infer Bot API 9.4 color style from text and callback semantics."""
    if (
        getattr(button, "url", None)
        or getattr(button, "web_app", None)
        or getattr(button, "login_url", None)
    ):
        return "primary"
    payload = " ".join(
        part
        for part in (button.text, getattr(button, "callback_data", None) or "")
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
        getattr(button, "callback_data", None)
        or getattr(button, "switch_inline_query", None)
        or getattr(button, "switch_inline_query_current_chat", None)
        or getattr(button, "switch_inline_query_chosen_chat", None)
        or getattr(button, "copy_text", None)
        or getattr(button, "callback_game", None)
        or getattr(button, "pay", None)
        or getattr(button, "request_users", None)
        or getattr(button, "request_chat", None)
        or getattr(button, "request_contact", None)
        or getattr(button, "request_location", None)
        or getattr(button, "request_poll", None)
        or getattr(button, "request_web_view", None)
    ):
        return "primary"
    return None


def _apply_button_style(button: InlineKeyboardButton | KeyboardButton) -> None:
    extra = cast(dict[str, Any] | None, getattr(button, "__pydantic_extra__"))
    if extra and extra.get("style"):
        return
    style = infer_button_style(button)
    if style:
        if extra is None:
            extra = {}
            setattr(button, "__pydantic_extra__", extra)
        extra["style"] = style


def apply_button_styles(
    markup: InlineKeyboardMarkup | ReplyKeyboardMarkup,
) -> InlineKeyboardMarkup | ReplyKeyboardMarkup:
    """Mutate markup with Bot API 9.4 style values while keeping aiogram 3.13 safe."""
    rows = (
        markup.inline_keyboard
        if isinstance(markup, InlineKeyboardMarkup)
        else markup.keyboard
    )
    for row in rows:
        for button in row:
            if isinstance(button, str):
                continue
            _apply_button_style(button)
    return markup


def install_button_style_patch() -> None:
    """Patch aiogram keyboard serialization once for project-wide colored buttons."""
    if getattr(InlineKeyboardBuilder, _PATCHED_ATTR, False):
        return

    original_as_markup = InlineKeyboardBuilder.as_markup
    original_reply_as_markup = ReplyKeyboardBuilder.as_markup
    original_model_dump = InlineKeyboardMarkup.model_dump
    original_reply_model_dump = ReplyKeyboardMarkup.model_dump
    original_button_model_dump = InlineKeyboardButton.model_dump
    original_keyboard_button_model_dump = KeyboardButton.model_dump
    original_model_dump_json = InlineKeyboardMarkup.model_dump_json
    original_reply_model_dump_json = ReplyKeyboardMarkup.model_dump_json
    original_button_model_dump_json = InlineKeyboardButton.model_dump_json
    original_keyboard_button_model_dump_json = KeyboardButton.model_dump_json
    original_markup_init = InlineKeyboardMarkup.__init__
    original_reply_markup_init = ReplyKeyboardMarkup.__init__
    original_button_init = InlineKeyboardButton.__init__
    original_keyboard_button_init = KeyboardButton.__init__

    def styled_markup_init(
        self: InlineKeyboardMarkup, *args: Any, **kwargs: Any
    ) -> None:
        original_markup_init(self, *args, **kwargs)
        apply_button_styles(self)

    def styled_reply_markup_init(
        self: ReplyKeyboardMarkup, *args: Any, **kwargs: Any
    ) -> None:
        original_reply_markup_init(self, *args, **kwargs)
        apply_button_styles(self)

    def styled_button_init(
        self: InlineKeyboardButton, *args: Any, **kwargs: Any
    ) -> None:
        original_button_init(self, *args, **kwargs)
        _apply_button_style(self)

    def styled_keyboard_button_init(
        self: KeyboardButton, *args: Any, **kwargs: Any
    ) -> None:
        original_keyboard_button_init(self, *args, **kwargs)
        _apply_button_style(self)

    def styled_as_markup(
        self: InlineKeyboardBuilder,
        **kwargs: Any,
    ) -> InlineKeyboardMarkup:
        markup = original_as_markup(self, **kwargs)
        return apply_button_styles(markup)

    def styled_reply_as_markup(
        self: ReplyKeyboardBuilder,
        **kwargs: Any,
    ) -> ReplyKeyboardMarkup:
        markup = original_reply_as_markup(self, **kwargs)
        return cast(ReplyKeyboardMarkup, apply_button_styles(markup))

    def styled_model_dump(
        self: InlineKeyboardMarkup,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        apply_button_styles(self)
        dumped = original_model_dump(self, *args, **kwargs)
        return cast(dict[str, Any], dumped)

    def styled_reply_model_dump(
        self: ReplyKeyboardMarkup,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        apply_button_styles(self)
        dumped = original_reply_model_dump(self, *args, **kwargs)
        return cast(dict[str, Any], dumped)

    def styled_button_model_dump(
        self: InlineKeyboardButton,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _apply_button_style(self)
        dumped = original_button_model_dump(self, *args, **kwargs)
        return cast(dict[str, Any], dumped)

    def styled_keyboard_button_model_dump(
        self: KeyboardButton,
        *args: Any,
        **kwargs: Any,
    ) -> dict[str, Any]:
        _apply_button_style(self)
        dumped = original_keyboard_button_model_dump(self, *args, **kwargs)
        return cast(dict[str, Any], dumped)

    def styled_model_dump_json(
        self: InlineKeyboardMarkup,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        apply_button_styles(self)
        return cast(str, original_model_dump_json(self, *args, **kwargs))

    def styled_reply_model_dump_json(
        self: ReplyKeyboardMarkup,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        apply_button_styles(self)
        return cast(str, original_reply_model_dump_json(self, *args, **kwargs))

    def styled_button_model_dump_json(
        self: InlineKeyboardButton,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        _apply_button_style(self)
        return cast(str, original_button_model_dump_json(self, *args, **kwargs))

    def styled_keyboard_button_model_dump_json(
        self: KeyboardButton,
        *args: Any,
        **kwargs: Any,
    ) -> str:
        _apply_button_style(self)
        return cast(
            str, original_keyboard_button_model_dump_json(self, *args, **kwargs)
        )

    builder_cls = cast(Any, InlineKeyboardBuilder)
    reply_builder_cls = cast(Any, ReplyKeyboardBuilder)
    markup_cls = cast(Any, InlineKeyboardMarkup)
    reply_markup_cls = cast(Any, ReplyKeyboardMarkup)
    button_cls = cast(Any, InlineKeyboardButton)
    keyboard_button_cls = cast(Any, KeyboardButton)
    markup_cls.__init__ = styled_markup_init
    reply_markup_cls.__init__ = styled_reply_markup_init
    button_cls.__init__ = styled_button_init
    keyboard_button_cls.__init__ = styled_keyboard_button_init
    builder_cls.as_markup = styled_as_markup
    reply_builder_cls.as_markup = styled_reply_as_markup
    markup_cls.model_dump = styled_model_dump
    reply_markup_cls.model_dump = styled_reply_model_dump
    button_cls.model_dump = styled_button_model_dump
    keyboard_button_cls.model_dump = styled_keyboard_button_model_dump
    markup_cls.model_dump_json = styled_model_dump_json
    reply_markup_cls.model_dump_json = styled_reply_model_dump_json
    button_cls.model_dump_json = styled_button_model_dump_json
    keyboard_button_cls.model_dump_json = styled_keyboard_button_model_dump_json
    setattr(InlineKeyboardBuilder, _PATCHED_ATTR, True)
