"""Тумблеры уведомлений в боте сохраняются, а не отвечают «Ошибка».

«Настройки → Уведомления» переключает поле запросом
`INSERT … ON CONFLICT(user_id) DO UPDATE SET поле = NOT поле`. В ветке
DO UPDATE голое имя колонки двусмысленно (строка таблицы или EXCLUDED), и
Postgres отвергал КАЖДОЕ нажатие: «column reference "new_user" is ambiguous».
Владелец видел «❌ Ошибка обновления настроек» на любом из девяти тумблеров,
то есть уведомления в боте не настраивались вообще. Найдено обходом всех
кнопок бота на живой базе 08.11.2026.

Первая проверка — без базы: каждое выражение обязано ссылаться на колонку
таблицы явно. Вторая гоняет все выражения на настоящем Postgres дважды и
сверяет, что значение действительно переключилось и вернулось.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from bot.handlers.botmother_menu import _NOTIF_SQL

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
OWNER = 993877


def test_every_toggle_names_the_table_column_explicitly():
    for field, expr in _NOTIF_SQL.items():
        assert expr.replace(" ", "") == f"{field}=NOTnotification_settings.{field}", (
            f"{field}: в ON CONFLICT DO UPDATE голое имя колонки двусмысленно — {expr!r}")


@pytest.mark.skipif(not DSN, reason="нужен INFRAGRAM_TEST_DSN")
def test_every_toggle_flips_the_value_on_real_postgres():
    async def _go():
        import asyncpg
        conn = await asyncpg.connect(DSN)
        try:
            await conn.execute("DELETE FROM notification_settings WHERE user_id=$1", OWNER)
            await conn.execute("INSERT INTO notification_settings(user_id) VALUES($1)", OWNER)
            for field, expr in _NOTIF_SQL.items():
                before = await conn.fetchval(
                    f"SELECT {field} FROM notification_settings WHERE user_id=$1", OWNER)
                for _ in range(2):
                    await conn.execute(
                        "INSERT INTO notification_settings(user_id) VALUES($1) "
                        f"ON CONFLICT(user_id) DO UPDATE SET {expr}, updated_at=now()",
                        OWNER)
                    now = await conn.fetchval(
                        f"SELECT {field} FROM notification_settings WHERE user_id=$1", OWNER)
                    assert now is (not before), field
                    before = now
        finally:
            await conn.execute("DELETE FROM notification_settings WHERE user_id=$1", OWNER)
            await conn.close()

    asyncio.run(_go())
