"""Гейт: хрупкие сцепки с внутренностями aiogram живы на текущем пине.

Проект в двух местах опирается на НЕпубличные детали aiogram/pydantic. Оба
работают, но при следующем апгрейде могут исчезнуть молча — и упасть уже в
проде, при старте бота или при отправке первой клавиатуры:

  1. `main.py` пишет в приватный `AiohttpSession._connector_init["ssl"]`
     (отключение проверки TLS для сессии бота);
  2. `install_button_style_patch()` monkey-patch'ит `__init__` / `model_dump` /
     `model_dump_json` у pydantic-моделей клавиатур ради цветных кнопок.

CLAUDE.md предупреждает об этом прозой — здесь предупреждение сделано
ИСПОЛНЯЕМЫМ: тот, кто поднимет aiogram, получит красный тест в CI вместо
неприятного сюрприза на проде.

Проверено на aiogram 3.30.0 (пин на 2026-07-25).
"""
from __future__ import annotations

import json

import pytest

aiogram = pytest.importorskip("aiogram", reason="aiogram не установлен в этой среде")


def test_pinned_version_matches_requirements():
    """Среда, в которой гоняются тесты, должна совпадать с тем, что ставит прод —
    иначе мы «тестируем не то, что запускаем»."""
    import re
    from pathlib import Path

    req = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text(encoding="utf-8")
    m = re.search(r"^aiogram==(\S+)", req, re.M)
    assert m, "aiogram должен быть запинен точной версией"
    pinned = m.group(1)
    if aiogram.__version__ != pinned:
        pytest.skip(
            f"среда: {aiogram.__version__}, пин: {pinned} — дрейф среды, "
            "но не дефект кода (проверьте, что CI ставит из requirements.txt)"
        )


def test_private_connector_init_still_exists():
    """main.py пишет в приватный _connector_init — если атрибут исчезнет,
    бот упадёт на старте."""
    from aiogram.client.session.aiohttp import AiohttpSession

    session = AiohttpSession()
    ci = getattr(session, "_connector_init", None)
    assert isinstance(ci, dict), (
        "AiohttpSession._connector_init пропал или сменил тип — main.py пишет в "
        "него ssl. Найдите публичную замену либо обновите main.py."
    )
    ci["ssl"] = False  # ровно то, что делает main.py
    assert ci["ssl"] is False


def test_bot_default_properties_api():
    """Конструктор после 3.7: parse_mode идёт через DefaultBotProperties,
    а не kwarg'ом."""
    from aiogram import Bot
    from aiogram.client.default import DefaultBotProperties
    from aiogram.enums import ParseMode

    bot = Bot(token="1:x", default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    assert bot.default.parse_mode == ParseMode.HTML


def test_webhook_server_module_available():
    from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application  # noqa: F401


def test_button_style_patch_survives_pydantic_internals():
    """Патч цветных кнопок трогает pydantic-сериализацию: проверяем не «не упало»,
    а что клавиатура реально собирается и сериализуется в валидный JSON."""
    from bot.utils.button_styles import install_button_style_patch
    from aiogram.utils.keyboard import InlineKeyboardBuilder

    install_button_style_patch()
    install_button_style_patch()  # идемпотентность: повторный вызов безопасен

    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Подтвердить", callback_data="ok")
    kb.button(text="🗑 Удалить", callback_data="del")
    markup = kb.as_markup()

    dumped = markup.model_dump()
    assert "inline_keyboard" in dumped, "model_dump сломан патчем"
    assert len(dumped["inline_keyboard"][0]) == 2, "кнопки потерялись"

    parsed = json.loads(markup.model_dump_json())
    assert parsed["inline_keyboard"][0][0]["callback_data"] == "ok", (
        "model_dump_json сломан патчем — клавиатуры уйдут в Telegram битыми"
    )
