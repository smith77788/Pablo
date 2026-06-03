from __future__ import annotations

import sys
from pathlib import Path

from aiogram.types import InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder


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
