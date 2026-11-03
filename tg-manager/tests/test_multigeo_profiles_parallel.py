"""Экран мультигео мини-аппа читает профили бота параллельно.

Было 33 запроса к Telegram подряд (11 языков × имя/описание/краткое) в одном
HTTP-запросе: секунды на открытие экрана, минуты при медленном Telegram.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from unittest.mock import patch

from services import bot_api, mini_app_bots

LANGS = ["", "ru", "en", "de", "fr", "es", "it", "uk", "pt", "zh", "ar"]


async def test_reads_run_concurrently_and_keep_language_order():
    async def _slow(field):
        async def fn(session, token, lc=""):
            await asyncio.sleep(0.1)
            if lc == "de" and field == "name":
                raise RuntimeError("сбой Telegram")
            return f"{field}-{lc}" if lc in ("", "ru", "de") else ""
        fn.__name__ = field
        return fn

    with patch.object(bot_api, "get_my_name", await _slow("name")), \
         patch.object(bot_api, "get_my_description", await _slow("desc")), \
         patch.object(bot_api, "get_my_short_description", await _slow("short")):
        t0 = time.monotonic()
        out = await bot_api.get_localized_profiles(None, "t", LANGS)
        took = time.monotonic() - t0

    assert took < 1.0, f"чтения идут подряд: {took:.1f} с (подряд было бы 3.3 с)"
    assert [p["lang"] for p in out] == ["default", "ru", "de"]
    assert out[2]["name"] == "" and out[2]["description"] == "desc-de", \
        "сбой одного поля — пустое поле, а не провал языка"


def test_miniapp_handler_uses_parallel_reader():
    src = inspect.getsource(mini_app_bots)
    body = src.split("async def multigeo_get", 1)[1].split("async def multigeo_set", 1)[0]
    assert "get_localized_profiles(" in body
    assert "get_my_name(" not in body
