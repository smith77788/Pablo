"""Тот же сторож, но на живой базе: проверяем САМ запрос, а не заглушку.

Юнит-тесты сторожа (`test_the_alert_channel_warns_before_it_goes_mute.py`)
ходят в поддельный пул, то есть проверяют решение, но не SQL. А решение здесь
зависит от двух настоящих запросов — `live_session_admins`
(`SELECT user_id FROM platform_admin_sessions WHERE expires_at > now()`) и
`MAX(expires_at)` в самом стороже. Переименуйте столбец, и оба молча вернут
«нечего предупреждать»: `_safe_fetchrow` проглотит ошибку, сторож выйдет, а
юнит-тесты останутся зелёными. Ратчет схемы такое не поймает — он проверяет
таблицы, а не столбцы.

Поэтому здесь сессия админки кладётся в настоящую таблицу, а `ADMIN_IDS`
очищается, чтобы получателем был ровно этот один истекающий доступ.
"""
from __future__ import annotations

import asyncio
import glob
import os
import re

import pytest

DSN = os.getenv("INFRAGRAM_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not DSN, reason="нужен живой Postgres: задайте INFRAGRAM_TEST_DSN")

OWNER = 991001

_LOOP: "asyncio.AbstractEventLoop | None" = None


def _run(coro):
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
        asyncio.set_event_loop(_LOOP)
    return _LOOP.run_until_complete(coro)


@pytest.fixture(scope="module")
def pool():
    import asyncpg

    async def _boot():
        conn = await asyncpg.connect(DSN)
        files = ["schema.sql"] + sorted(
            glob.glob("schema_v*.sql"),
            key=lambda p: int(re.search(r"schema_v(\d+)", p).group(1)))
        for f in files:
            try:
                await conn.execute(open(f, encoding="utf-8").read())
            except Exception:
                pass  # схемы идемпотентны
        await conn.close()
        return await asyncpg.create_pool(DSN, min_size=1, max_size=4)

    try:
        p = _run(_boot())
    except Exception as exc:
        pytest.skip(f"Postgres по INFRAGRAM_TEST_DSN недоступен: {str(exc)[:120]}")
    yield p
    _run(p.close())


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append((int(chat_id), str(text)))


@pytest.fixture(autouse=True)
def _clean(pool, monkeypatch):
    # Получателем должен быть только истекающий доступ: постоянных нет.
    monkeypatch.setenv("ADMIN_IDS", "")
    # ЧИСТИМ ВСЕ сессии, а не только свою: сторож смотрит на MAX(expires_at),
    # то есть на срок ПОСЛЕДНЕГО живого доступа — канал замолкает, когда
    # истечёт он. Чужая строка с 30-дневным сроком (её кладёт
    # test_admin_session_shared) отодвигает максимум за окно предупреждения, и
    # тест падает на верном коде. На этом сам и попался.
    _run(pool.execute("DELETE FROM platform_admin_sessions"))
    # Кэш сессий в процессе бота не должен подмешивать своих.
    import bot.handlers.admin as _adm
    _adm._session_admins.clear()
    # Анти-повтор уведомлений ПЕРСИСТЕНТНЫЙ (notification_dedup, окно сутки):
    # без очистки тест проходит ровно один раз в сутки, а потом «ломается» на
    # исправном коде. Таблицу чистим по своему ключу, не целиком.
    _run(pool.execute(
        "DELETE FROM notification_dedup WHERE dedup_key = $1",
        "alert-channel-expiry"))
    yield


def _grant(pool, days: float) -> None:
    _run(pool.execute(
        """INSERT INTO platform_admin_sessions(user_id, granted_at, expires_at)
           VALUES($1, now(), now() + make_interval(secs => $2))
           ON CONFLICT (user_id) DO UPDATE
           SET granted_at = now(), expires_at = EXCLUDED.expires_at""",
        OWNER, float(days) * 86400.0))


def test_the_real_queries_find_the_expiring_access(pool):
    """Доступ истекает через двое суток — предупреждение уходит по живой базе."""
    from services import op_worker

    _grant(pool, 2)
    bot = _Bot()
    _run(op_worker._watchdog_alert_channel_expiry(pool, bot))
    assert bot.sent, (
        "на живой базе сторож никого не нашёл: либо запрос к "
        "platform_admin_sessions не тот (столбец переименован — ошибку "
        "проглотил _safe_fetchrow), либо получатели считаются иначе"
    )
    assert bot.sent[0][0] == OWNER
    assert "ADMIN_IDS" in bot.sent[0][1]


def test_a_far_away_expiry_stays_quiet_on_a_real_base(pool):
    """Самопроверка: доступ ещё на месяц — молчим, и это не «запрос не работает»."""
    from services import op_worker

    _grant(pool, 29)
    bot = _Bot()
    _run(op_worker._watchdog_alert_channel_expiry(pool, bot))
    assert not bot.sent


def test_no_session_at_all_is_not_this_watchdogs_case(pool):
    """Получателей нет вовсе — это счётчик protective_alerts_no_recipient."""
    from services import op_worker

    bot = _Bot()
    _run(op_worker._watchdog_alert_channel_expiry(pool, bot))
    assert not bot.sent
