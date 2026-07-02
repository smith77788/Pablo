from __future__ import annotations

import json
import sys
from pathlib import Path

from aiogram.types import InlineKeyboardButton, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tg-manager"))

from bot.utils.button_styles import infer_button_style, install_button_style_patch


def test_infer_button_style_from_button_semantics() -> None:
    assert (
        infer_button_style(
            InlineKeyboardButton(text="❌ Удалить", callback_data="user:delete")
        )
        == "danger"
    )
    assert (
        infer_button_style(
            InlineKeyboardButton(text="✅ Запустить", callback_data="task:start")
        )
        == "success"
    )
    assert (
        infer_button_style(
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="bot:settings")
        )
        == "primary"
    )


def test_installed_patch_styles_builder_markup_dump() -> None:
    install_button_style_patch()
    kb = InlineKeyboardBuilder()
    kb.button(text="💰 Выдать подписку", callback_data="adm:grant_ask")
    kb.button(text="🚫 Заблокировать", callback_data="adm:block_ask")

    markup = kb.as_markup()
    dumped = markup.model_dump(exclude_none=True)

    assert dumped["inline_keyboard"][0][0]["style"] == "success"
    assert dumped["inline_keyboard"][0][1]["style"] == "danger"


def test_neutral_interactive_buttons_fallback_to_primary() -> None:
    assert (
        infer_button_style(
            InlineKeyboardButton(
                text="🗂️ Реестр активов",
                callback_data="infra:asset_registry",
            )
        )
        == "primary"
    )


def test_infra_dashboard_buttons_are_primary() -> None:
    labels = (
        "🗂️ Реестр ассетов",
        "❤️ Здоровье аккаунтов",
        "⚡ Флуд-защита и лимиты",
        "📋 Лог операций",
        "📊 Статистика за сегодня",
        "🎯 Возможности аккаунтов",
        "🔄 Авто-балансировка пулов",
        "🎯 Советник",
        "🧠 Copilot",
        "🔬 Intelligence Report",
    )

    for label in labels:
        assert (
            infer_button_style(
                InlineKeyboardButton(text=label, callback_data="infra:menu")
            )
            == "primary"
        )


def test_reply_keyboard_buttons_receive_styles() -> None:
    install_button_style_patch()
    kb = ReplyKeyboardBuilder()
    kb.button(text="✅ Запустить")
    kb.button(text="❌ Отмена")
    kb.button(text="⚙️ Настройки")

    dumped = kb.as_markup(resize_keyboard=True).model_dump(exclude_none=True)

    assert dumped["keyboard"][0][0]["style"] == "success"
    assert dumped["keyboard"][0][1]["style"] == "danger"
    assert dumped["keyboard"][0][2]["style"] == "primary"


def test_direct_reply_keyboard_markup_dump_is_styled() -> None:
    install_button_style_patch()
    markup = ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text="➕ Добавить"), KeyboardButton(text="◀️ Назад")]]
    )

    dumped = markup.model_dump(exclude_none=True)

    assert dumped["keyboard"][0][0]["style"] == "success"
    assert dumped["keyboard"][0][1]["style"] == "primary"


def test_direct_inline_keyboard_markup_dump_is_styled() -> None:
    install_button_style_patch()
    markup = InlineKeyboardBuilder()
    markup.row(InlineKeyboardButton(text="✅ Запустить", callback_data="task:start"))
    dumped = markup.as_markup().model_dump(exclude_none=True)

    assert dumped["inline_keyboard"][0][0]["style"] == "success"


def test_direct_inline_keyboard_markup_constructor_is_styled() -> None:
    install_button_style_patch()
    from aiogram.types import InlineKeyboardMarkup

    markup = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📁 Реестр ассетов", callback_data="assets"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="cancel"),
            ]
        ]
    )

    dumped = markup.model_dump(exclude_none=True)

    assert dumped["inline_keyboard"][0][0]["style"] == "primary"
    assert dumped["inline_keyboard"][0][1]["style"] == "danger"


def test_markup_json_dump_is_styled() -> None:
    install_button_style_patch()
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="task:confirm")
    kb.button(text="❌ Отмена", callback_data="task:cancel")

    dumped = json.loads(kb.as_markup().model_dump_json(exclude_none=True))

    assert dumped["inline_keyboard"][0][0]["style"] == "success"
    assert dumped["inline_keyboard"][0][1]["style"] == "danger"


def _assert_all_buttons_styled(dumped: dict) -> None:
    rows = dumped.get("inline_keyboard") or dumped.get("keyboard") or []
    assert rows, "keyboard has no rows"
    missing = [
        button.get("text", "")
        for row in rows
        for button in row
        if "style" not in button
    ]
    assert missing == []


def test_project_inline_keyboards_are_styled() -> None:
    install_button_style_patch()

    from bot.keyboards import bot_menu, bots_list, main_menu

    _assert_all_buttons_styled(main_menu(is_admin=True).model_dump(exclude_none=True))
    _assert_all_buttons_styled(
        bots_list(
            [
                {
                    "bot_id": 1,
                    "username": "demo_bot",
                    "first_name": "Demo",
                    "audience_count": 12,
                },
                {
                    "bot_id": 2,
                    "username": None,
                    "first_name": "Second",
                    "audience_count": 0,
                },
            ],
            page=0,
        ).model_dump(exclude_none=True)
    )
    _assert_all_buttons_styled(
        bot_menu(1, username="demo_bot").model_dump(exclude_none=True)
    )
